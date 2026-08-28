"""Exact arithmetic helpers shared by execution implementations."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from quant_data_kit import FixedPoint
from quant_data_kit.exceptions import ValidationError


def decimal(value: FixedPoint) -> Decimal:
    if not isinstance(value, FixedPoint):
        raise ValidationError("value must be a FixedPoint")
    return value.to_decimal()


def fixed(
    value: Decimal | int | str,
    scale: int,
    *,
    rounding: str = ROUND_HALF_EVEN,
) -> FixedPoint:
    return FixedPoint.from_decimal(Decimal(str(value)), scale, rounding=rounding)


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
