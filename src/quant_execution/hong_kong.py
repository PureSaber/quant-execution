"""Daily HKD cash-equity research execution using the shared exact ledger.

One aggregate fill per order, whole board lots, no shorting or borrowing.
This is an opening-price research model, not an intraday matching model.
The supplied settlement calendar is independent from the trading calendar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from quant_data_kit import AssetClass, FixedPoint, InstrumentSpec, MarkPriceEvent
from quant_data_kit.exceptions import ValidationError

from quant_execution.contracts import Fee, Fill, Side
from quant_execution.ledger import ExactAccountLedger


def money(value: Decimal) -> FixedPoint:
    return FixedPoint.from_decimal(value, 2, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class HKFeeSchedule:
    valid_from: date
    valid_to: date
    commission_rate: Decimal
    minimum_commission: Decimal
    platform_fee: Decimal
    stamp_rate: Decimal
    sfc_rate: Decimal
    afrc_rate: Decimal
    trading_rate: Decimal
    settlement_rate: Decimal
    settlement_minimum: Decimal
    settlement_maximum: Decimal | None
    slippage_rate: Decimal
    source: str

    def __post_init__(self):
        if self.valid_to < self.valid_from or not self.source.strip():
            raise ValueError("Fee schedule needs valid dates and provenance")
        for key in self.__dataclass_fields__:
            value = getattr(self, key)
            if key not in {"valid_from", "valid_to", "source", "settlement_maximum"} and (
                not isinstance(value, Decimal) or not value.is_finite() or value < 0
            ):
                raise ValueError(f"Invalid fee parameter: {key}")
        if self.settlement_maximum is not None and (
            not self.settlement_maximum.is_finite()
            or self.settlement_maximum < self.settlement_minimum
        ):
            raise ValueError("Invalid settlement maximum")

    def charge(
        self, notional: FixedPoint, day: date, *, stamp_exempt: bool
    ) -> dict[str, FixedPoint]:
        if not isinstance(stamp_exempt, bool):
            raise TypeError("Stamp exemption must be explicitly boolean")
        amount = notional.to_decimal()
        if amount <= 0 or not self.valid_from <= day <= self.valid_to:
            raise ValueError(
                "Positive notional and a fee schedule covering the trade date required"
            )
        clearing = max(self.settlement_minimum, amount * self.settlement_rate)
        if self.settlement_maximum is not None:
            clearing = min(clearing, self.settlement_maximum)
        values = {
            "commission": max(self.minimum_commission, amount * self.commission_rate),
            "platform": self.platform_fee,
            "stamp": Decimal(0)
            if stamp_exempt
            else (amount * self.stamp_rate).quantize(Decimal(1), rounding=ROUND_CEILING),
            "sfc": amount * self.sfc_rate,
            "afrc": amount * self.afrc_rate,
            "trading": amount * self.trading_rate,
            "settlement": clearing,
            "slippage": amount * self.slippage_rate,
        }
        return {key: money(value) for key, value in values.items()}


class HKDailyExecution:
    sends_live_orders = False

    def __init__(
        self,
        instruments: dict[str, InstrumentSpec],
        *,
        initial_cash: FixedPoint,
        opened_at: datetime,
        fees: HKFeeSchedule,
        settlement_days: list[date],
    ):
        if not instruments or initial_cash.to_decimal() <= 0:
            raise ValueError("HK execution needs instruments and positive capital")
        if sorted(set(settlement_days)) != settlement_days:
            raise ValueError("Settlement calendar must be unique and sorted")
        for spec in instruments.values():
            if (
                spec.venue != "XHKG"
                or spec.settlement_currency != "HKD"
                or spec.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
            ):
                raise ValueError("HK daily execution only accepts XHKG/HKD instruments")
            lot = int(spec.metadata["lot_size"])
            if lot <= 0 or spec.metadata["stamp_exempt"] not in {"true", "false"}:
                raise ValueError("Explicit board lot and stamp exemption required")
        self.instruments = instruments
        self.fees = fees
        self.settlement_days = settlement_days
        self.ledger = ExactAccountLedger(
            account_id="hk-research",
            base_currency="HKD",
            instruments=instruments,
            initial_cash={"HKD": initial_cash},
            money_scale=8,
            opened_at=opened_at,
        )
        self.pending: list[tuple[date, Decimal]] = []
        self.executed: dict[str, tuple[tuple, Fill, dict]] = {}
        self.last_settlement_day: date | None = None

    def available_cash(self) -> Decimal:
        return self.ledger.cash_balance("HKD") - sum((x[1] for x in self.pending), Decimal(0))

    def settle_end_of_day(self, day: date) -> None:
        # Settlement occurs at day end. Proceeds cannot fund morning trades on T+2.
        if self.last_settlement_day is not None and day < self.last_settlement_day:
            raise ValueError("Settlement date cannot move backwards")
        self.pending = [item for item in self.pending if item[0] > day]
        self.last_settlement_day = day

    def settlement_date(self, day: date) -> date:
        future = [d for d in self.settlement_days if d > day]
        if len(future) < 2:
            raise ValueError("Settlement calendar does not cover T+2")
        return future[1]

    def costs(self, symbol: str, price: FixedPoint, quantity: int, day: date) -> dict:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        spec = self.instruments[symbol]
        return self.fees.charge(
            FixedPoint.from_decimal(price.to_decimal() * quantity, 8),
            day,
            stamp_exempt=spec.metadata["stamp_exempt"] == "true",
        )

    def affordable_quantity(
        self, symbol: str, price: FixedPoint, budget: Decimal, day: date
    ) -> int:
        if price.to_decimal() <= 0 or not budget.is_finite() or budget < 0:
            raise ValueError("Invalid price/budget")
        lot = int(self.instruments[symbol].metadata["lot_size"])
        low, high = 0, int(budget / price.to_decimal()) // lot
        while low < high:
            mid = (low + high + 1) // 2
            costs = self.costs(symbol, price, mid * lot, day)
            total = price.to_decimal() * mid * lot + sum(
                (v.to_decimal() for v in costs.values()), Decimal(0)
            )
            if total <= budget:
                low = mid
            else:
                high = mid - 1
        return low * lot

    def execute(
        self,
        *,
        order_id: str,
        symbol: str,
        quantity: int,
        side: Side,
        price: FixedPoint,
        at: datetime,
    ) -> tuple[Fill, dict]:
        signature = (symbol, quantity, side, price, at)
        if order_id in self.executed:
            prior, fill, charges = self.executed[order_id]
            if signature != prior:
                raise ValueError("Order ID reused with different content")
            return fill, dict(charges)
        if not isinstance(side, Side) or not order_id.strip():
            raise ValueError("A side and nonempty order ID are required")
        spec = self.instruments[symbol]
        if not spec.effective_from <= at or (spec.effective_to and at >= spec.effective_to):
            raise ValueError("Instrument rules do not cover execution time")
        if spec.available_at > at:
            raise ValueError("Instrument rules were unavailable at execution time")
        day = at.date()  # HK opening/closing research events are on the same UTC date.
        if self.last_settlement_day is not None and day <= self.last_settlement_day:
            raise ValueError("Cannot trade on a session already settled at day end")
        charges = self.costs(symbol, price, quantity, day)
        if quantity % int(spec.metadata["lot_size"]):
            raise ValueError("Order is not a whole board lot")
        amount = price.to_decimal() * quantity
        fee_amount = sum((v.to_decimal() for v in charges.values()), Decimal(0))
        snapshot = self.ledger.snapshot(at)
        held = snapshot.positions.get(symbol, FixedPoint(0, 0)).to_decimal()
        if side is Side.SELL and quantity > held:
            raise ValueError("Short selling is outside HK cash-research scope")
        if side is Side.BUY and amount + fee_amount > self.available_cash():
            raise ValueError("Insufficient settled cash including fees")
        if side is Side.SELL and self.available_cash() + amount < fee_amount:
            raise ValueError("Sale proceeds cannot cover fees")
        due = self.settlement_date(day)
        fill = Fill(
            fill_id=order_id,
            order_id=order_id,
            account_id="hk-research",
            strategy_id="hk-daily",
            instrument_id=symbol,
            side=side,
            quantity=FixedPoint(quantity, 0),
            price=price,
            event_time=at,
        )
        checkpoint = self.ledger.capture_state()
        try:
            self.ledger.apply_with_trading_day(fill, trading_day=day)
            for key, value in charges.items():
                if value.units:
                    self.ledger.apply(
                        Fee(
                            fee_id=f"{order_id}:{key}",
                            fill_id=order_id,
                            account_id="hk-research",
                            amount=value,
                            currency="HKD",
                            event_time=at,
                            fee_type=f"hk:{key}",
                        )
                    )
        except Exception:
            self.ledger.restore_state(checkpoint)
            raise
        if side is Side.SELL:
            self.pending.append((due, max(Decimal(0), amount - fee_amount)))
        self.executed[order_id] = (signature, fill, dict(charges))
        return fill, charges

    def mark(self, symbol: str, price: FixedPoint, at: datetime) -> None:
        self.ledger.mark(
            MarkPriceEvent(
                event_id=f"mark:{symbol}:{at.isoformat()}",
                instrument_id=symbol,
                event_time=at,
                received_at=at,
                available_at=at,
                source="hk-daily-research",
                trading_day=at.date(),
                session_id=at.date().isoformat(),
                sequence=0,
                price=price,
            )
        )


def reject_generic_hk_rule(spec: InstrumentSpec) -> None:
    if spec.venue.upper() in {"XHKG", "HKEX", "SEHK"}:
        raise ValidationError(
            "HK securities require HKDailyExecution with dated fees and settlement calendar; "
            "the generic rule book does not implement Hong Kong intraday matching"
        )
