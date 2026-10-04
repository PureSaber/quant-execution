"""Exact arithmetic helpers shared by execution implementations."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from fractions import Fraction

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


def sum_decimal_exact(values: Iterable[Decimal], start: Decimal = Decimal(0)) -> Decimal:
    """Sum finite decimals exactly without consulting the ambient context."""

    total = start
    for value in values:
        total = add_decimal_exact(total, value)
    return total


def decimal_fraction(value: Decimal | FixedPoint | int) -> Fraction:
    """Return the exact rational value of a finite decimal-like value."""

    if isinstance(value, FixedPoint):
        return Fraction(value.units, 10**value.scale)
    if isinstance(value, int) and not isinstance(value, bool):
        return Fraction(value)
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError("exact fraction conversion requires a finite decimal value")
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    exponent = int(parts.exponent)
    return (
        Fraction(coefficient * 10**exponent)
        if exponent >= 0
        else Fraction(coefficient, 10 ** (-exponent))
    )


def fraction_decimal_exact(value: Fraction) -> Decimal:
    """Convert a finite base-10 fraction to Decimal without rounding."""

    if not isinstance(value, Fraction):
        raise ValidationError("value must be a Fraction")
    numerator = value.numerator
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValidationError("exact decimal result is non-terminating")
    scale = max(twos, fives)
    coefficient = abs(numerator) * 2 ** (scale - twos) * 5 ** (scale - fives)
    digits = tuple(int(digit) for digit in str(coefficient)) if coefficient else (0,)
    return Decimal((int(numerator < 0), digits, -scale))


def multiply_decimal_exact(*values: Decimal | FixedPoint | int) -> Decimal:
    """Multiply finite decimal-like values exactly."""

    result = Fraction(1)
    for value in values:
        result *= decimal_fraction(value)
    return fraction_decimal_exact(result)


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
