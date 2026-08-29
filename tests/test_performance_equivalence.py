from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from quant_data_kit import FixedPoint

from quant_execution import OrderIntent, OrderType, Side, TimeInForce
from quant_execution._json import flat_sequence_bytes
from quant_execution.broker import _intent_bytes
from quant_execution.schemas import execution_payload


def test_flat_sequence_encoder_matches_historical_json_for_all_supported_paths() -> None:
    cases = (
        ("ascii", 7, None, True, False),
        ("中文", 'line\nquote"', -3),
        ("fallback", Decimal("1.2300")),
    )
    for values in cases:
        expected = json.dumps(
            values,
            ensure_ascii=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        assert flat_sequence_bytes(values) == expected


def test_intent_hot_serializer_is_byte_identical_for_unicode_and_optional_fields() -> None:
    intent = OrderIntent(
        idempotency_key="订单-一",
        account_id="账户",
        strategy_id="策略",
        instrument_id="crypto:test:BTCUSDT",
        side=Side.BUY,
        quantity=FixedPoint(1, 3),
        order_type=OrderType.STOP_LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        limit_price=FixedPoint(10_000, 2),
        stop_price=FixedPoint(10_100, 2),
        reduce_only=True,
    )
    expected = json.dumps(
        execution_payload(intent),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert _intent_bytes(intent) == expected
