from dataclasses import replace

import pandas as pd
import pytest
from conftest import fp
from quant_data_kit.exceptions import ValidationError
from test_broker import intent
from test_dividend_lifecycle import EX_AT, basis, entitlement, ledger, lifecycle, request

from quant_execution.artifacts import (
    ArrowReplayArtifactSink,
    export_dividend_run,
    replay_dividend_run,
)
from quant_execution.broker import DeterministicBroker
from quant_execution.dividends import DividendEntitlementBasis, DividendExecutionPhase


def test_us_cash_events_and_cutoff_keep_nanoseconds():
    from test_us_cash import account

    from quant_execution.us_cash import settled_cash

    value = account(commission_bps=0, slippage_bps=0)
    stamp = pd.Timestamp("2024-05-24T14:00:00.000000900Z")
    value.mark("US:A", 100, stamp, "mark")
    assert pd.Timestamp(value.ledger.snapshot().event_time).value == stamp.value
    value.trade("US:A", 1, 100, stamp + pd.Timedelta(1, unit="ns"), "buy")
    with pytest.raises(ValueError, match="precede"):
        settled_cash(value.ledger, stamp)
    assert value.buying_power(stamp + pd.Timedelta(1, unit="ns")) == 900


def test_corporate_action_keeps_exact_effective_time():
    from test_financial_actions import account, terms

    value = account()
    action = terms("split", ratio="2")
    stamp = pd.Timestamp(action.effective_at) + pd.Timedelta(900, unit="ns")
    action = replace(action, effective_at=stamp.isoformat())
    result = value.apply_corporate_action(action, at=stamp)
    assert pd.Timestamp(result.event_time).value == stamp.value


@pytest.mark.parametrize("offset", [1, 899, 900, 999, 1000])
def test_entitlement_basis_json_round_trip_preserves_exact_instant(offset):
    stamp = pd.Timestamp(EX_AT) + pd.Timedelta(offset, unit="ns")
    original = basis(available_at=stamp, captured_at=stamp)
    restored = DividendEntitlementBasis.from_dict(original.to_dict())
    assert pd.Timestamp(restored.available_at).value == stamp.value
    assert restored.to_dict() == original.to_dict()


def test_basis_json_rejects_sub_nanosecond_instead_of_truncating():
    payload = basis().to_dict()
    payload["available_at"] = "2026-01-01T00:00:00.0000009001Z"
    with pytest.raises(ValidationError):
        DividendEntitlementBasis.from_dict(payload)


def test_streamed_terminal_order_and_cancel_retry_keep_nanoseconds(tmp_path):
    stamp = pd.Timestamp("2026-01-02T00:00:00.000000900Z")
    broker = DeterministicBroker()
    sink = ArrowReplayArtifactSink(tmp_path / "orders", batch_size=1)
    broker.start_artifact_stream(sink)
    original = replace(intent(), created_at=stamp)
    accepted = broker.submit(original)
    cancel_at = stamp + pd.Timedelta(1, unit="ns")
    cancelled = broker.cancel(accepted.order_id, idempotency_key="cancel", created_at=cancel_at)
    restored = broker.get_order(accepted.order_id)
    assert pd.Timestamp(restored.intent.created_at).value == stamp.value
    assert broker.submit(original) == restored
    repeated = broker.cancel(accepted.order_id, idempotency_key="cancel", created_at=cancel_at)
    assert repeated == cancelled
    assert pd.Timestamp(repeated.event_time).value == cancel_at.value
    broker.finish_artifact_stream()
    sink.close({"run_id": "nanosecond-broker"})


def test_future_dividend_fact_one_nanosecond_after_cutoff_is_rejected():
    cutoff = pd.Timestamp(EX_AT) + pd.Timedelta(899, unit="ns")
    value = lifecycle(terms=entitlement(available_at=cutoff + pd.Timedelta(1, unit="ns")))
    account = ledger()
    before = account.capture_state()
    with pytest.raises(ValidationError):
        account.apply_dividend_lifecycle(
            request(
                value, DividendExecutionPhase.ENTITLEMENT, cutoff=cutoff, evidence_basis=basis()
            )
        )
    assert account.capture_state() == before


def test_export_and_replay_preserve_nanosecond_cash_journal(tmp_path):
    account = ledger()
    stamp = pd.Timestamp(EX_AT) + pd.Timedelta(900, unit="ns")
    account.book_external_cash(
        transfer_id="nanosecond-deposit", amount=fp("1"), currency="HKD", event_time=stamp
    )
    account.record_dividend_valuation(as_of=stamp + pd.Timedelta(1, unit="ns"))
    stored = export_dividend_run(account, tmp_path / "cash")
    replayed = replay_dividend_run(stored).ledger
    assert replayed.journal_sha256 == account.journal_sha256
    assert replayed.snapshot().nav == account.snapshot().nav
    assert (
        pd.Timestamp(replayed.snapshot().event_time).value
        == pd.Timestamp(account.snapshot().event_time).value
    )
