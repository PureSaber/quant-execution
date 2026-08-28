"""Instrument-driven asset rules and deterministic pre/in-run risk gates."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from quant_data_kit import (
    AssetClass,
    BarEvent,
    FixedPoint,
    InstrumentSpec,
    MarketEvent,
    MarkPriceEvent,
    QuoteEvent,
    StatusEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError

from quant_execution._fixed import aligned, decimal, fixed
from quant_execution.contracts import (
    AccountSnapshot,
    Fee,
    Fill,
    LiquidityRole,
    Order,
    OrderIntent,
    RiskDecision,
    Side,
)
from quant_execution.ledger import ExactAccountLedger


def _metadata_decimal(
    spec: InstrumentSpec,
    key: str,
    *,
    required: bool = False,
    default: str = "0",
) -> Decimal:
    raw = spec.metadata.get(key)
    if raw is None:
        if required:
            raise ValidationError(f"InstrumentSpec metadata {key!r} is required")
        raw = default
    try:
        value = Decimal(raw)
    except Exception as exc:
        raise ValidationError(f"InstrumentSpec metadata {key!r} must be decimal") from exc
    if not value.is_finite():
        raise ValidationError(f"InstrumentSpec metadata {key!r} must be finite")
    return value


@dataclass(frozen=True)
class MarketState:
    event: MarketEvent
    reference_price: FixedPoint | None
    status: str


class _AssetRule:
    code = "GENERIC"

    def check(
        self,
        intent: OrderIntent,
        snapshot: AccountSnapshot,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> RiskDecision:
        del ledger
        price = _intent_price(intent, state)
        if price is None:
            return RiskDecision(False, "NO_REFERENCE_PRICE", "market order has no visible price")
        upper = spec.metadata.get("daily_upper_limit")
        lower = spec.metadata.get("daily_lower_limit")
        if upper is not None and price > Decimal(upper):
            return RiskDecision(False, "PRICE_ABOVE_LIMIT", "price exceeds instrument upper limit")
        if lower is not None and price < Decimal(lower):
            return RiskDecision(False, "PRICE_BELOW_LIMIT", "price is below instrument lower limit")
        return RiskDecision(True, "ACCEPTED")

    def fee_rate(
        self,
        fill: Fill,
        order: Order,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> Decimal:
        del order, state, ledger
        key = "maker_fee_rate" if fill.liquidity_role is LiquidityRole.MAKER else "taker_fee_rate"
        return _metadata_decimal(spec, key, default="0")


class AShareRule(_AssetRule):
    code = "A_SHARE"

    def check(
        self,
        intent: OrderIntent,
        snapshot: AccountSnapshot,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> RiskDecision:
        base = super().check(intent, snapshot, state, spec, ledger)
        if not base.accepted:
            return base
        lot_size = _metadata_decimal(spec, "lot_size", required=True)
        if decimal(intent.quantity) % lot_size:
            return RiskDecision(False, "A_SHARE_LOT", "quantity is not an A-share board lot")
        quantity = decimal(intent.quantity)
        position = decimal(snapshot.positions.get(intent.instrument_id, FixedPoint(0, 0)))
        if intent.side is Side.SELL:
            acquired = ledger.acquired_today(intent.instrument_id, state.event.trading_day)
            if quantity > position - acquired:
                return RiskDecision(
                    False, "A_SHARE_T_PLUS_ONE", "sell exceeds settled T+1 position"
                )
        if intent.side is Side.BUY:
            price = _intent_price(intent, state)
            cash = decimal(snapshot.cash_balances.get(spec.settlement_currency, FixedPoint(0, 0)))
            if price is not None and cash < quantity * price * decimal(spec.contract_multiplier):
                return RiskDecision(False, "INSUFFICIENT_CASH", "cash cannot fund A-share buy")
        return RiskDecision(True, "ACCEPTED")

    def fee_rate(
        self,
        fill: Fill,
        order: Order,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> Decimal:
        rate = _metadata_decimal(spec, "commission_rate", default="0")
        if fill.side is Side.SELL:
            rate += _metadata_decimal(spec, "stamp_duty_rate", required=True)
        return rate


class FuturesRule(_AssetRule):
    code = "CN_FUTURE"

    def check(
        self,
        intent: OrderIntent,
        snapshot: AccountSnapshot,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> RiskDecision:
        base = super().check(intent, snapshot, state, spec, ledger)
        if not base.accepted:
            return base
        position = decimal(snapshot.positions.get(intent.instrument_id, FixedPoint(0, 0)))
        signed = decimal(intent.quantity) * (1 if intent.side is Side.BUY else -1)
        if intent.reduce_only and (
            position == 0 or position * signed >= 0 or abs(signed) > abs(position)
        ):
            return RiskDecision(False, "REDUCE_ONLY_VIOLATION", "order would not reduce position")
        if (
            spec.expiry_date is not None
            and state.event.trading_day >= spec.expiry_date
            and not intent.reduce_only
        ):
            return RiskDecision(False, "FUTURES_EXPIRY", "opening is disabled at expiry")
        if not intent.reduce_only:
            price = _intent_price(intent, state)
            required = (
                decimal(intent.quantity)
                * price
                * decimal(spec.contract_multiplier)
                * _metadata_decimal(spec, "initial_margin_rate", required=True)
            )
            available = decimal(snapshot.nav) - decimal(snapshot.initial_margin)
            if required > available:
                return RiskDecision(False, "INSUFFICIENT_MARGIN", "initial margin is insufficient")
        return RiskDecision(True, "ACCEPTED")

    def fee_rate(
        self,
        fill: Fill,
        order: Order,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> Decimal:
        if order.intent.reduce_only and ledger.acquired_today(
            fill.instrument_id, state.event.trading_day
        ):
            return _metadata_decimal(spec, "close_today_fee_rate", required=True)
        return _metadata_decimal(spec, "fee_rate", default="0")


class CryptoSpotRule(_AssetRule):
    code = "CRYPTO_SPOT"

    def check(
        self,
        intent: OrderIntent,
        snapshot: AccountSnapshot,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> RiskDecision:
        base = super().check(intent, snapshot, state, spec, ledger)
        if not base.accepted:
            return base
        minimum = _metadata_decimal(spec, "min_quantity", required=True)
        quantity = decimal(intent.quantity)
        if quantity < minimum:
            return RiskDecision(False, "MIN_QUANTITY", "quantity is below venue minimum")
        if intent.reduce_only:
            return RiskDecision(False, "SPOT_REDUCE_ONLY", "reduce_only is derivative-only")
        if intent.side is Side.SELL:
            position = decimal(snapshot.positions.get(intent.instrument_id, FixedPoint(0, 0)))
            if quantity > position:
                return RiskDecision(False, "INSUFFICIENT_POSITION", "spot sell exceeds balance")
        else:
            price = _intent_price(intent, state)
            cash = decimal(
                snapshot.cash_balances.get(
                    spec.quote_currency or spec.settlement_currency, FixedPoint(0, 0)
                )
            )
            worst_rate = max(
                _metadata_decimal(spec, "maker_fee_rate", default="0"),
                _metadata_decimal(spec, "taker_fee_rate", default="0"),
            )
            required = quantity * price * decimal(spec.contract_multiplier) * (1 + worst_rate)
            if required > cash:
                return RiskDecision(False, "INSUFFICIENT_CASH", "quote balance is insufficient")
        return RiskDecision(True, "ACCEPTED")


class LinearPerpetualRule(FuturesRule):
    code = "LINEAR_PERPETUAL"

    def check(
        self,
        intent: OrderIntent,
        snapshot: AccountSnapshot,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> RiskDecision:
        minimum = _metadata_decimal(spec, "min_quantity", required=True)
        if decimal(intent.quantity) < minimum:
            return RiskDecision(False, "MIN_QUANTITY", "quantity is below venue minimum")
        return super().check(intent, snapshot, state, spec, ledger)

    def fee_rate(
        self,
        fill: Fill,
        order: Order,
        state: MarketState,
        spec: InstrumentSpec,
        ledger: ExactAccountLedger,
    ) -> Decimal:
        return _AssetRule.fee_rate(self, fill, order, state, spec, ledger)


class RuleBookRiskGate:
    """Fail-closed gate backed only by InstrumentSpec, MarketEvent and ledger state."""

    sends_live_orders = False

    def __init__(
        self,
        *,
        instruments: Mapping[str, InstrumentSpec],
        ledger: ExactAccountLedger,
        money_scale: int = 8,
    ) -> None:
        self.instruments = dict(instruments)
        self.ledger = ledger
        self.money_scale = money_scale
        self.reset()

    def reset(self) -> None:
        self._states: dict[str, MarketState] = {}
        self._cash_reservations: dict[str, tuple[str, Decimal, Decimal]] = {}
        self._margin_reservations: dict[str, tuple[Decimal, Decimal]] = {}

    def observe(self, event: MarketEvent) -> None:
        prior = self._states.get(event.instrument_id)
        status = prior.status if prior is not None else "open"
        reference = prior.reference_price if prior is not None else None
        if isinstance(event, StatusEvent):
            status = event.status.lower()
        elif isinstance(event, (MarkPriceEvent, TradeEvent)):
            reference = event.price
        elif isinstance(event, QuoteEvent):
            midpoint = (decimal(event.bid_price) + decimal(event.ask_price)) / 2
            reference = fixed(midpoint, event.bid_price.scale)
        elif isinstance(event, BarEvent):
            reference = event.close_price
        self._states[event.instrument_id] = MarketState(event, reference, status)

    def check(self, order_intent: OrderIntent, account_snapshot: AccountSnapshot) -> RiskDecision:
        if order_intent.account_id != account_snapshot.account_id:
            return RiskDecision(False, "ACCOUNT_MISMATCH", "intent targets another account")
        spec = self.instruments.get(order_intent.instrument_id)
        state = self._states.get(order_intent.instrument_id)
        if spec is None:
            return RiskDecision(False, "UNKNOWN_INSTRUMENT", "InstrumentSpec is missing")
        if state is None:
            return RiskDecision(False, "NO_MARKET_STATE", "no MarketEvent has been observed")
        if order_intent.created_at < spec.available_at:
            return RiskDecision(False, "PIT_INSTRUMENT", "InstrumentSpec was not yet available")
        if order_intent.created_at < spec.effective_from or (
            spec.effective_to is not None and order_intent.created_at >= spec.effective_to
        ):
            return RiskDecision(False, "INSTRUMENT_INACTIVE", "instrument lifecycle is inactive")
        if state.status in {"halted", "suspended", "closed"}:
            return RiskDecision(False, "MARKET_NOT_TRADABLE", state.status)
        if state.status == "limit_up" and order_intent.side is Side.BUY:
            return RiskDecision(False, "LIMIT_UP", "buy is blocked at limit-up")
        if state.status == "limit_down" and order_intent.side is Side.SELL:
            return RiskDecision(False, "LIMIT_DOWN", "sell is blocked at limit-down")
        if not aligned(order_intent.quantity, spec.quantity_step):
            return RiskDecision(False, "QUANTITY_STEP", "quantity violates InstrumentSpec step")
        for field_name in ("limit_price", "stop_price"):
            value = getattr(order_intent, field_name)
            if value is not None and not aligned(value, spec.price_tick):
                return RiskDecision(False, "PRICE_TICK", f"{field_name} violates price tick")
        try:
            decision = self._rule(spec).check(
                order_intent, account_snapshot, state, spec, self.ledger
            )
            if not decision.accepted:
                return decision
            return self._check_reservations(order_intent, account_snapshot, state, spec)
        except ValidationError as exc:
            return RiskDecision(False, "RULE_CONFIGURATION", str(exc))

    def reserve(self, intent: OrderIntent) -> None:
        spec = self.instruments[intent.instrument_id]
        state = self._states[intent.instrument_id]
        cash, margin = self._reservation_requirement(intent, state, spec)
        if cash is not None:
            prior = self._cash_reservations.get(intent.idempotency_key)
            expected = (cash[0], cash[1], cash[1])
            if prior is not None and prior != expected:
                raise ValidationError("reservation key reused with different cash requirement")
            self._cash_reservations[intent.idempotency_key] = expected
        if margin:
            prior_margin = self._margin_reservations.get(intent.idempotency_key)
            expected_margin = (margin, margin)
            if prior_margin is not None and prior_margin != expected_margin:
                raise ValidationError("reservation key reused with different margin requirement")
            self._margin_reservations[intent.idempotency_key] = expected_margin

    def release_fill(self, fill: Fill, order: Order) -> None:
        fraction = decimal(fill.quantity) / decimal(order.intent.quantity)
        key = order.intent.idempotency_key
        if key in self._cash_reservations:
            currency, amount, original = self._cash_reservations[key]
            remaining = amount - original * fraction
            if remaining <= 0:
                del self._cash_reservations[key]
            else:
                self._cash_reservations[key] = (currency, remaining, original)
        if key in self._margin_reservations:
            amount, original = self._margin_reservations[key]
            remaining = amount - original * fraction
            if remaining <= 0:
                del self._margin_reservations[key]
            else:
                self._margin_reservations[key] = (remaining, original)

    def release_order(self, order: Order) -> None:
        self._cash_reservations.pop(order.intent.idempotency_key, None)
        self._margin_reservations.pop(order.intent.idempotency_key, None)

    def runtime_check(self, snapshot: AccountSnapshot) -> RiskDecision:
        if snapshot.liquidation_required:
            return RiskDecision(
                False,
                "LIQUIDATION_REQUIRED",
                "NAV is at or below aggregate maintenance margin",
            )
        return RiskDecision(True, "ACCEPTED")

    def fee_for(self, fill: Fill, order: Order) -> Fee | None:
        spec = self.instruments[fill.instrument_id]
        state = self._states[fill.instrument_id]
        rate = self._rule(spec).fee_rate(fill, order, state, spec, self.ledger)
        amount = (
            decimal(fill.quantity) * decimal(fill.price) * decimal(spec.contract_multiplier) * rate
        )
        if amount == 0:
            return None
        raw = f"{fill.fill_id}|{rate}|{spec.settlement_currency}".encode()
        return Fee(
            fee_id=f"fee-{hashlib.sha256(raw).hexdigest()[:24]}",
            fill_id=fill.fill_id,
            account_id=fill.account_id,
            amount=fixed(amount, self.money_scale),
            currency=spec.settlement_currency,
            event_time=fill.event_time,
            fee_type=("maker" if fill.liquidity_role is LiquidityRole.MAKER else "taker"),
        )

    def _check_reservations(
        self,
        intent: OrderIntent,
        snapshot: AccountSnapshot,
        state: MarketState,
        spec: InstrumentSpec,
    ) -> RiskDecision:
        cash, margin = self._reservation_requirement(intent, state, spec)
        if cash is not None:
            currency, required = cash
            reserved = sum(
                amount
                for key, (reserved_currency, amount, original) in self._cash_reservations.items()
                if reserved_currency == currency and key != intent.idempotency_key
            )
            available = decimal(
                snapshot.cash_balances.get(currency, FixedPoint(0, self.money_scale))
            )
            if required + reserved > available:
                return RiskDecision(
                    False,
                    "INSUFFICIENT_AVAILABLE_CASH",
                    "open-order reservations exceed available cash",
                )
        if margin:
            reserved_margin = sum(
                amount
                for key, (amount, original) in self._margin_reservations.items()
                if key != intent.idempotency_key
            )
            available_margin = decimal(snapshot.nav) - decimal(snapshot.initial_margin)
            if margin + reserved_margin > available_margin:
                return RiskDecision(
                    False,
                    "INSUFFICIENT_AVAILABLE_MARGIN",
                    "open-order reservations exceed available margin",
                )
        return RiskDecision(True, "ACCEPTED")

    def _reservation_requirement(
        self, intent: OrderIntent, state: MarketState, spec: InstrumentSpec
    ) -> tuple[tuple[str, Decimal] | None, Decimal]:
        price = _intent_price(intent, state)
        if price is None:
            raise ValidationError("reservation requires a reference price")
        notional = decimal(intent.quantity) * price * decimal(spec.contract_multiplier)
        product = spec.product_type.lower()
        derivative = (
            spec.asset_class is AssetClass.FUTURE or "perpetual" in product or "perp" in product
        )
        if derivative and not intent.reduce_only:
            settlement_margin = notional * _metadata_decimal(
                spec, "initial_margin_rate", required=True
            )
            return (
                None,
                self.ledger.convert_to_base(
                    settlement_margin,
                    spec.settlement_currency,
                    event_time=state.event.available_at,
                ),
            )
        if not derivative and intent.side is Side.BUY:
            rates = (
                _metadata_decimal(spec, "commission_rate", default="0"),
                _metadata_decimal(spec, "maker_fee_rate", default="0"),
                _metadata_decimal(spec, "taker_fee_rate", default="0"),
            )
            currency = spec.quote_currency or spec.settlement_currency
            return (currency, notional * (Decimal(1) + max(rates))), Decimal(0)
        return None, Decimal(0)

    @staticmethod
    def _rule(spec: InstrumentSpec) -> _AssetRule:
        product = spec.product_type.lower()
        if spec.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
            return AShareRule()
        if spec.asset_class is AssetClass.FUTURE:
            return FuturesRule()
        if spec.asset_class is AssetClass.CRYPTO and ("perpetual" in product or "perp" in product):
            return LinearPerpetualRule()
        if spec.asset_class is AssetClass.CRYPTO and "spot" in product:
            return CryptoSpotRule()
        raise ValidationError(
            f"unsupported asset rule for {spec.asset_class.value}/{spec.product_type}"
        )


def _intent_price(intent: OrderIntent, state: MarketState) -> Decimal | None:
    if intent.limit_price is not None:
        return decimal(intent.limit_price)
    if intent.stop_price is not None:
        return decimal(intent.stop_price)
    return decimal(state.reference_price) if state.reference_price is not None else None
