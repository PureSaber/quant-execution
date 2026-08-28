from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from quant_data_kit import AssetClass, FixedPoint, InstrumentSpec, MarginMode

UTC = timezone.utc
T0 = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)


def fp(value: str | int, scale: int = 2) -> FixedPoint:
    return FixedPoint.from_decimal(Decimal(str(value)), scale)


def spec(
    instrument_id: str,
    *,
    asset_class: AssetClass,
    product_type: str,
    settlement_currency: str,
    base_currency: str | None = None,
    quote_currency: str | None = None,
    price_tick: str = "0.01",
    quantity_step: str = "1",
    multiplier: str = "1",
    margin_mode: MarginMode = MarginMode.NONE,
    metadata: dict[str, str] | None = None,
) -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=instrument_id,
        asset_class=asset_class,
        product_type=product_type,
        venue="TEST",
        native_symbol=instrument_id,
        base_currency=base_currency,
        quote_currency=quote_currency,
        settlement_currency=settlement_currency,
        price_tick=fp(price_tick, len(price_tick.split(".")[1]) if "." in price_tick else 0),
        quantity_step=fp(
            quantity_step,
            len(quantity_step.split(".")[1]) if "." in quantity_step else 0,
        ),
        contract_multiplier=fp(
            multiplier, len(multiplier.split(".")[1]) if "." in multiplier else 0
        ),
        calendar_id="TEST-CALENDAR",
        margin_mode=margin_mode,
        effective_from=T0 - timedelta(days=365),
        available_at=T0 - timedelta(days=365),
        metadata=metadata or {},
    )


def event_fields(
    event_id: str,
    instrument_id: str,
    *,
    seconds: int = 0,
    sequence: int | None = None,
    trading_day: date = date(2026, 1, 2),
) -> dict[str, object]:
    at = T0 + timedelta(seconds=seconds)
    return {
        "event_id": event_id,
        "instrument_id": instrument_id,
        "event_time": at,
        "received_at": at,
        "available_at": at,
        "source": "fixture",
        "trading_day": trading_day,
        "session_id": f"session:{trading_day.isoformat()}",
        "sequence": seconds if sequence is None else sequence,
    }
