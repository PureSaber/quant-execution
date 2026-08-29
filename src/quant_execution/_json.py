"""Exact small JSON encoders used by deterministic identifier hot paths."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from quant_data_kit import FixedPoint


def flat_sequence_bytes(values: Sequence[object]) -> bytes:
    """Encode a flat scalar sequence byte-identically to the historical JSON settings."""

    tokens: list[str] = []
    for value in values:
        if isinstance(value, str):
            tokens.append(json.encoder.encode_basestring_ascii(value))
        elif value is None:
            tokens.append("null")
        elif value is True:
            tokens.append("true")
        elif value is False:
            tokens.append("false")
        elif isinstance(value, int):
            tokens.append(str(value))
        else:
            return json.dumps(
                values,
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
            ).encode()
    return ("[" + ",".join(tokens) + "]").encode()


def string_token(value: str) -> str:
    """Return the exact ensure_ascii JSON token for one validated string."""

    return json.encoder.encode_basestring_ascii(value)


def fixed_token(value: FixedPoint | None) -> str:
    """Encode a fixed-point value like sorted canonical execution JSON."""

    if value is None:
        return "null"
    return f'{{"scale":{value.scale},"units":{value.units}}}'


def utc_token(value: datetime, *, zulu: bool = True) -> str:
    """Encode one already validated UTC timestamp as a JSON string token."""

    rendered = value.isoformat()
    if zulu:
        rendered = rendered.replace("+00:00", "Z")
    return string_token(rendered)
