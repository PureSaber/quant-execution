from dataclasses import replace
from decimal import Decimal

import pytest
from quant_data_kit.exceptions import ValidationError
from test_engine import engine_for, fp, scenario_a_share, scenario_crypto, scenario_future


@pytest.mark.parametrize("factory", [scenario_a_share, scenario_crypto, scenario_future])
def test_segmentation_preserves_open_orders_cash_fees_and_exact_ledger(factory):
    one, events = factory()
    expected = one.replay(events, seed=0)
    many, events = factory()
    ordered = sorted(events, key=lambda e: e.available_at)
    actual = many.replay_segments([[event] for event in ordered], seed=0)
    assert actual == expected
    assert many.artifacts == one.artifacts
    assert many.ledger.snapshot() == one.ledger.snapshot()
    assert many.strategy.calls == len(events)


def test_invalid_segment_order_fails_before_account_mutation():
    engine, events = scenario_a_share()
    before = engine.ledger.snapshot()
    with pytest.raises(ValidationError, match="chronological"):
        engine.replay_segments([[events[0]], [events[1]]], seed=0)
    assert engine.ledger.snapshot() == before
    with pytest.raises(ValidationError, match="at least one"):
        engine.replay_segments([], seed=0)


def test_fixed_order_higher_cost_differential_against_hand_cash_account():
    low, events = scenario_a_share()
    low.replay(events, seed=0)
    high, events = scenario_a_share()
    instrument = next(iter(high.ledger.instruments))
    original = high.ledger.instruments[instrument]
    changed = replace(original, metadata={**original.metadata, "commission_rate": "0.02"})
    high = engine_for(
        run_id=high.run_id,
        registry={instrument: changed},
        initial_cash={"CNY": fp("100000")},
        base_currency="CNY",
        strategy=high.strategy,
    )
    high.replay(events, seed=0)
    assert len(high.artifacts.fills) == len(low.artifacts.fills) == 1
    low_fill, high_fill = low.artifacts.fills[0], high.artifacts.fills[0]
    assert low_fill.quantity == high_fill.quantity and low_fill.price == high_fill.price
    for engine in (low, high):
        fill = engine.artifacts.fills[0]
        paid = sum((fee.amount.to_decimal() for fee in engine.artifacts.fees), Decimal(0))
        expected = Decimal(100000) - fill.quantity.to_decimal() * fill.price.to_decimal() - paid
        assert engine.ledger.cash_balance("CNY") == expected
    assert high.ledger.snapshot().nav.to_decimal() < low.ledger.snapshot().nav.to_decimal()
