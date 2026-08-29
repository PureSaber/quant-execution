from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from quant_data_kit import FixedPoint

from quant_execution import (
    ExactAccountLedger,
    Fill,
    LedgerEventType,
    LedgerTransaction,
    LiquidityRole,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    OrderType,
    Posting,
    Side,
    TimeInForce,
)
from quant_execution._json import flat_sequence_bytes
from quant_execution.broker import _intent_bytes
from quant_execution.engine import _fact_hash, _hash
from quant_execution.ledger import _canonical as ledger_canonical
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


def test_fact_stream_hashes_are_byte_identical_to_historical_canonical_json() -> None:
    at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    order_event = OrderEvent(
        event_id="事件-一",
        order_id="订单-一",
        event_time=at,
        sequence=2,
        from_status=OrderStatus.ACCEPTED,
        to_status=OrderStatus.FILLED,
        fill_quantity=FixedPoint(1, 3),
    )
    fill = Fill(
        fill_id="成交-一",
        order_id="订单-一",
        account_id="账户",
        strategy_id="策略",
        instrument_id="crypto:test:BTCUSDT",
        side=Side.BUY,
        quantity=FixedPoint(1, 3),
        price=FixedPoint(10_000, 2),
        event_time=at,
        liquidity_role=LiquidityRole.TAKER,
        venue_trade_id=None,
    )
    assert _fact_hash([order_event]) == _hash([execution_payload(order_event)])
    assert _fact_hash([fill]) == _hash([execution_payload(fill)])
    assert _fact_hash([]) == _hash([])

    fallback = OrderIntent(
        idempotency_key="fallback",
        account_id="账户",
        strategy_id="策略",
        instrument_id="crypto:test:BTCUSDT",
        side=Side.BUY,
        quantity=FixedPoint(1, 3),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        created_at=at,
    )
    assert _fact_hash([fallback]) == _hash([execution_payload(fallback)])


def test_streaming_ledger_hash_matches_historical_nested_payload() -> None:
    at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={},
        initial_cash={"USDT": FixedPoint(10_000, 2)},
        opened_at=at,
    )
    transaction = LedgerTransaction(
        transaction_id="tx-多尺度",
        idempotency_key="manual-1",
        event_time=at,
        event_type=LedgerEventType.FEE,
        reference_id="fee-一",
        postings=(
            Posting(
                ledger_account="assets:cash",
                currency="USDT",
                amount=FixedPoint(-100, 2),
            ),
            Posting(
                ledger_account="expenses:fees",
                currency="USDT",
                amount=FixedPoint(1, 0),
                instrument_id="crypto:test:BTCUSDT",
            ),
        ),
    )
    expected_transaction = json.dumps(
        execution_payload(transaction),
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    assert ledger._transaction_bytes(transaction) == expected_transaction

    historical_payload = {
        "transactions": [execution_payload(item) for item in ledger.transactions],
        "marks": [],
        "fx_snapshots": [
            {
                "currency": currency,
                "rate": str(rate),
                "event_time": event_time.isoformat(),
                "version": index + 1,
            }
            for index, (currency, rate, event_time) in enumerate(ledger._fx_history)
        ],
    }
    expected_hash = hashlib.sha256(ledger_canonical(historical_payload)).hexdigest()
    assert ledger.journal_sha256 == expected_hash
