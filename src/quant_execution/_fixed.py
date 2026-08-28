"""Exact arithmetic helpers shared by execution implementations."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from functools import lru_cache

from quant_data_kit import FixedPoint
from quant_data_kit.exceptions import ValidationError


@lru_cache(maxsize=8192)
def decimal(value: FixedPoint) -> Decimal:
    if not isinstance(value, FixedPoint):
        raise ValidationError("value must be a FixedPoint")
    return value.to_decimal()


@lru_cache(maxsize=8192)
def fixed(
    value: Decimal | int | str,
    scale: int,
    *,
    rounding: str | None = ROUND_HALF_EVEN,
) -> FixedPoint:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValidationError("fixed-point value must be finite")
    scaled = decimal_value.scaleb(scale)
    integral = scaled.to_integral_value(rounding=rounding) if rounding else scaled
    if rounding is None and scaled != scaled.to_integral_value():
        raise ValidationError(f"value {value!r} is not exact at scale {scale}")
    return FixedPoint(units=int(integral), scale=scale)


def floor_to_scale(value: Decimal, scale: int) -> FixedPoint:
    return fixed(value, scale, rounding=ROUND_DOWN)


def aligned(value: FixedPoint, step: FixedPoint) -> bool:
    return decimal(value) % decimal(step) == 0


def remaining_units(total: FixedPoint, filled: FixedPoint) -> int:
    if total.scale != filled.scale:
        raise ValidationError("quantity scales differ")
    return total.units - filled.units


def canonical_fixed(value: FixedPoint) -> tuple[int, int]:
    return value.units, value.scale
