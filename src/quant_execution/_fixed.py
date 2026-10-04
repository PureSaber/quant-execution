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
    rounding: str | None = ROUND_HALF_EVEN,
) -> FixedPoint:
    return FixedPoint.from_decimal(value, scale, rounding=rounding)


def add_decimal_exact(left: Decimal, right: Decimal) -> Decimal:
    """Add finite decimals exactly without consulting the ambient context."""

    if not isinstance(left, Decimal) or not isinstance(right, Decimal):
        raise ValidationError("exact decimal addition requires Decimal values")
    if not left.is_finite() or not right.is_finite():
        raise ValidationError("exact decimal addition requires finite values")

    left_parts = left.as_tuple()
    right_parts = right.as_tuple()
    exponent = min(int(left_parts.exponent), int(right_parts.exponent))

    def aligned(parts) -> int:
        coefficient = 0
        for digit in parts.digits:
            coefficient = coefficient * 10 + digit
        coefficient *= 10 ** (int(parts.exponent) - exponent)
        return -coefficient if parts.sign and coefficient else coefficient

    total = aligned(left_parts) + aligned(right_parts)
    magnitude = abs(total)
    digits = tuple(int(digit) for digit in str(magnitude)) if magnitude else (0,)
    return Decimal((int(total < 0), digits, exponent))


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
