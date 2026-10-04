from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import fp
from quant_data_kit import CorporateActionEvent, MarkPriceEvent, market_event_payload
from quant_data_kit.exceptions import ValidationError
from test_dividend_lifecycle import (
    EX_AT,
    apply_entitlement,
    basis,
    certified_policy,
    election,
    entitlement,
    ledger,
    lifecycle,
    payment,
    pit_rate,
    request,
    same_currency_prefix,
)

import quant_execution.artifacts as artifacts_module
from quant_execution.artifacts import (
    ArrowReplayArtifactSink,
    export_dividend_run,
    load_stored_artifacts,
    replay_dividend_run,
)
from quant_execution.contracts import Fee, Fill, Funding, LiquidityRole, Settlement, Side
from quant_execution.dividends import DividendExecutionMode, DividendExecutionPhase
from quant_execution.schemas import execution_payload

UTC = timezone.utc


def completed_ledger():
    account = ledger()
    prefix = same_currency_prefix(account)
    account.apply_dividend_lifecycle(
        request(
            replace(prefix, payment=payment(gross="3", net="3")),
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )
    return account


def test_manifest_1_1_replays_initial_conditions_transactions_phases_and_journal(
    tmp_path: Path,
) -> None:
    account = completed_ledger()
    before = account.capture_state()
    root = tmp_path / "dividend-run"

    stored = export_dividend_run(account, root)
    replayed = replay_dividend_run(root)

    assert stored.schema_version == "1.1.0"
    assert set(stored.counts) == {
        "orders",
        "order_events",
        "fills",
        "fees",
        "settlements",
        "ledger_transactions",
        "risk_events",
        "dividend_records",
    }
    assert stored.counts["dividend_records"] == 3
    replayed_state = replayed.ledger.capture_state()
    before.pop("posting_cache")
    replayed_state.pop("posting_cache")
    assert replayed_state == before
    assert replayed.ledger.journal_sha256 == account.journal_sha256
    assert replayed.ledger.snapshot().nav == account.snapshot().nav
    assert stored.run_metadata["market_admission_certified"] is False


def test_full_pit_fx_observation_order_and_explicit_valuation_replay(tmp_path: Path) -> None:
    account = ledger()
    initial = lifecycle(terms=entitlement(declared="USD", options=("USD",)))
    apply_entitlement(account, initial)
    first_at = EX_AT - timedelta(minutes=2)
    second_at = EX_AT - timedelta(minutes=1)
    first = account.observe_pit_fx(pit_rate("first", base="USD", rate="7.7", observed_at=first_at))
    second = account.observe_pit_fx(
        pit_rate("second", base="USD", rate="7.8", observed_at=second_at)
    )
    valuation = account.record_dividend_valuation(as_of=EX_AT)

    stored = export_dividend_run(account, tmp_path / "fx-run")
    replayed = replay_dividend_run(stored).ledger

    assert [item.rate_payload for item in replayed._dividend_pit_fx_records] == [
        first.rate_payload,
        second.rate_payload,
    ]
    assert replayed._recorded_dividend_valuations[valuation.valuation_idempotency_key] == valuation
    assert replayed.snapshot(EX_AT).nav == account.snapshot(EX_AT).nav


def test_production_export_marks_certification_and_replay_requires_verifier(
    tmp_path: Path,
) -> None:
    class Verifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return basis.certification_ref == "certified" and bool(lifecycle.dividend_id)

    verifier = Verifier()
    account = ledger(mode=DividendExecutionMode.PRODUCTION_CERTIFIED, verifier=verifier)
    apply_entitlement(account, evidence_basis=basis(certification_ref="certified"))
    stored = export_dividend_run(account, tmp_path / "production")

    assert stored.run_metadata["market_admission_certified"] is False
    assert stored.run_metadata["trusted_verification_scope"] == ["entitlement_basis"]
    with pytest.raises(ValidationError, match="trusted verifier"):
        replay_dividend_run(stored)
    assert (
        replay_dividend_run(stored, entitlement_evidence_verifier=verifier).ledger.journal_sha256
        == account.journal_sha256
    )


def test_production_manifest_reports_only_independently_verified_fact_scope(
    tmp_path: Path,
) -> None:
    class FullVerifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return basis.certification_ref == "certified"

        def verify_payment_policy(self, *, policy, lifecycle) -> bool:
            return policy.policy_id == "policy-1"

        def verify_dividend_payment(self, *, payment, lifecycle) -> bool:
            return payment.account_id == "account"

    verifier = FullVerifier()
    account = ledger(mode=DividendExecutionMode.PRODUCTION_CERTIFIED, verifier=verifier)
    initial = lifecycle()
    apply_entitlement(
        account,
        initial,
        evidence_basis=basis(certification_ref="certified"),
    )
    prefix = replace(
        initial,
        election=election("HKD"),
        payment_policy=certified_policy(),
    )
    account.apply_dividend_lifecycle(
        request(
            prefix,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )
    account.apply_dividend_lifecycle(
        request(
            replace(prefix, payment=payment(gross="3", net="3")),
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )

    stored = export_dividend_run(account, tmp_path / "production-full")

    assert stored.run_metadata["market_admission_certified"] is False
    assert stored.run_metadata["trusted_verification_scope"] == [
        "entitlement_basis",
        "payment_policy",
        "actual_dividend_payment",
    ]
    assert (
        replay_dividend_run(stored, entitlement_evidence_verifier=verifier).ledger.journal_sha256
        == account.journal_sha256
    )


def test_replay_preserves_ordered_marks_fill_valuations_lots_and_payment(
    tmp_path: Path,
) -> None:
    account = ledger()
    prefix = same_currency_prefix(account)
    mark_1_at = EX_AT + timedelta(days=1, hours=1)
    mark_2_at = EX_AT + timedelta(days=1, hours=3)
    account.mark(
        MarkPriceEvent(
            event_id="mark-1",
            instrument_id=prefix.instrument_id,
            event_time=mark_1_at,
            received_at=mark_1_at,
            available_at=mark_1_at,
            source="test-mark-source",
            trading_day=date(2026, 1, 3),
            session_id="session-1",
            sequence=1,
            price=fp("11"),
        ),
        create_snapshot=False,
    )
    valuation_1 = account.record_dividend_valuation(as_of=mark_1_at)
    account.apply(
        Fill(
            fill_id="sell-1",
            order_id="order-sell-1",
            account_id="account",
            strategy_id="strategy",
            instrument_id=prefix.instrument_id,
            side=Side.SELL,
            quantity=fp("1", 0),
            price=fp("12"),
            event_time=EX_AT + timedelta(days=1, hours=2),
            liquidity_role=LiquidityRole.TAKER,
        ),
        create_snapshot=False,
    )
    account.apply(
        Fee(
            fee_id="fee-sell-1",
            fill_id="sell-1",
            account_id="account",
            amount=fp("0.01"),
            currency="HKD",
            event_time=EX_AT + timedelta(days=1, hours=2, minutes=1),
            fee_type="commission",
        ),
        create_snapshot=False,
    )
    account.mark(
        MarkPriceEvent(
            event_id="mark-2",
            instrument_id=prefix.instrument_id,
            event_time=mark_2_at,
            received_at=mark_2_at,
            available_at=mark_2_at,
            source="test-mark-source",
            trading_day=date(2026, 1, 3),
            session_id="session-1",
            sequence=2,
            price=fp("13"),
        ),
        create_snapshot=False,
    )
    valuation_2 = account.record_dividend_valuation(as_of=mark_2_at)
    account.apply_dividend_lifecycle(
        request(
            replace(prefix, payment=payment(gross="3", net="3")),
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )

    stored = export_dividend_run(account, tmp_path / "ordered-business-facts")
    replayed = replay_dividend_run(stored).ledger

    assert (
        replayed._recorded_dividend_valuations[valuation_1.valuation_idempotency_key] == valuation_1
    )
    assert (
        replayed._recorded_dividend_valuations[valuation_2.valuation_idempotency_key] == valuation_2
    )
    assert replayed._position_lots == account._position_lots
    assert replayed._marks == account._marks
    assert replayed._dividend_lifecycle_states == account._dividend_lifecycle_states
    assert replayed.transactions == account.transactions
    assert replayed.journal_sha256 == account.journal_sha256
    expected_state = account.capture_state()
    actual_state = replayed.capture_state()
    expected_state.pop("posting_cache")
    actual_state.pop("posting_cache")
    assert actual_state == expected_state


def test_export_reads_only_the_sealed_snapshot_after_capture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    account = completed_ledger()
    expected_cash = account.cash_balance("HKD")
    original_capture = account.capture_dividend_export_state

    def capture_then_mutate():
        captured = original_capture()
        account.book_external_cash(
            transfer_id="after-seal",
            amount=fp("1"),
            currency="HKD",
            event_time=EX_AT + timedelta(days=3),
        )
        return captured

    monkeypatch.setattr(account, "capture_dividend_export_state", capture_then_mutate)
    stored = export_dividend_run(account, tmp_path / "sealed-snapshot")
    replayed = replay_dividend_run(stored).ledger

    assert account.cash_balance("HKD") == expected_cash + fp("1").to_decimal()
    assert replayed.cash_balance("HKD") == expected_cash


def test_dividend_replay_payload_parsers_cover_all_supported_event_shapes() -> None:
    at = EX_AT + timedelta(days=5)
    fee = Fee(
        fee_id="fee",
        fill_id="fill",
        account_id="account",
        amount=fp("0.01"),
        currency="HKD",
        event_time=at,
        fee_type="commission",
    )
    funding = Funding(
        funding_id="funding",
        account_id="account",
        instrument_id="HK:DIVIDEND",
        amount=fp("0.10"),
        currency="HKD",
        event_time=at,
    )
    settlement = Settlement(
        settlement_id="settlement",
        account_id="account",
        instrument_id="HK:DIVIDEND",
        amount=fp("0.20"),
        currency="HKD",
        event_time=at,
        settlement_type="cash_adjustment",
        settlement_price=fp("12"),
    )
    action = CorporateActionEvent(
        event_id="action",
        instrument_id="HK:DIVIDEND",
        event_time=at,
        received_at=at,
        available_at=at,
        source="issuer",
        trading_day=at.date(),
        session_id="session",
        sequence=1,
        action_type="split",
        effective_date=at.date(),
        ratio=fp("2"),
    )
    cash_action = replace(
        action,
        event_id="cash-action",
        action_type="cash_dividend_entitlement",
        ratio=None,
        cash_amount=fp("1"),
        currency="HKD",
    )

    for kind, event, payload in (
        ("fee", fee, execution_payload(fee)),
        ("funding", funding, execution_payload(funding)),
        ("settlement", settlement, execution_payload(settlement)),
        ("corporate_action", action, market_event_payload(action)),
        ("corporate_action", cash_action, market_event_payload(cash_action)),
    ):
        assert (
            artifacts_module._ledger_event_from_business_fact(
                {"event_kind": kind, "event": payload}
            )
            == event
        )

    no_price = replace(settlement, settlement_id="settlement-no-price", settlement_price=None)
    assert (
        artifacts_module._ledger_event_from_business_fact(
            {"event_kind": "settlement", "event": execution_payload(no_price)}
        )
        == no_price
    )

    rich_spec = replace(
        ledger().instruments["HK:DIVIDEND"],
        effective_to=EX_AT + timedelta(days=365),
        superseded_at=EX_AT + timedelta(days=366),
        expiry_date=date(2027, 1, 1),
    )
    assert (
        artifacts_module._spec_from_payload(artifacts_module._spec_payload(rich_spec)) == rich_spec
    )


def test_dividend_replay_payload_parsers_reject_malformed_values() -> None:
    for value, message in (
        (None, "fixed-point object"),
        ({"units": True, "scale": 2}, "units must be an integer"),
        ({"units": 1, "scale": True}, "scale must be an integer"),
    ):
        with pytest.raises(ValidationError, match=message):
            artifacts_module._fixed_from_payload(value, "value")

    for value, message in (
        (None, "ISO-8601 timestamp"),
        ("not-a-time", "ISO-8601 timestamp"),
        ("2026-01-01T00:00:00", "timezone-aware"),
    ):
        with pytest.raises(ValidationError, match=message):
            artifacts_module._time_from_payload(value, "value")

    with pytest.raises(ValidationError, match="instrument spec replay fact"):
        artifacts_module._spec_from_payload(None)
    with pytest.raises(ValidationError, match="ledger transaction replay fact"):
        artifacts_module._transaction_from_payload(None)
    with pytest.raises(ValidationError, match="ledger event business fact"):
        artifacts_module._ledger_event_from_business_fact(None)
    with pytest.raises(ValidationError, match="ledger event payload"):
        artifacts_module._ledger_event_from_business_fact({"event_kind": "fee"})
    with pytest.raises(ValidationError, match="unsupported ledger event"):
        artifacts_module._ledger_event_from_business_fact({"event_kind": "unknown", "event": {}})
    with pytest.raises(ValidationError, match="mark business fact must"):
        artifacts_module._mark_from_business_fact(None)
    with pytest.raises(ValidationError, match="invalid event type"):
        artifacts_module._mark_from_business_fact({"event_type": "trade"})

    with pytest.raises(ValidationError, match="explicit execution mode"):
        artifacts_module._dividend_run_metadata(SimpleNamespace(dividend_execution_mode=None))
    with pytest.raises(ValidationError, match="requires EVIDENCED_PIT"):
        artifacts_module._dividend_run_metadata(
            SimpleNamespace(
                dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
                fx_valuation_mode=artifacts_module.FxValuationMode.LEGACY,
            )
        )
    with pytest.raises(ValidationError, match="requires lifecycle records"):
        artifacts_module._dividend_run_metadata(
            SimpleNamespace(
                dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
                fx_valuation_mode=artifacts_module.FxValuationMode.EVIDENCED_PIT,
                _dividend_operation_log=[],
            )
        )
    assert (
        artifacts_module._trusted_verification_scope(
            SimpleNamespace(
                dividend_execution_mode=DividendExecutionMode.PRODUCTION_CERTIFIED,
                _dividend_execution_records=[],
            )
        )
        == []
    )


def _replace_metadata(stored, metadata):
    unsigned = deepcopy(metadata)
    unsigned.pop("metadata_sha256", None)
    metadata["metadata_sha256"] = artifacts_module.hashlib.sha256(
        artifacts_module.canonical_bytes(unsigned)
    ).hexdigest()
    return replace(stored, run_metadata=metadata)


def test_replay_rejects_malformed_ordered_business_fact_metadata(tmp_path: Path) -> None:
    stored = export_dividend_run(completed_ledger(), tmp_path / "business-fact-guards")
    cases = []

    missing = deepcopy(stored.run_metadata)
    missing.pop("business_facts")
    cases.append((missing, "ordered business facts are missing"))
    wrong_item = deepcopy(stored.run_metadata)
    wrong_item["business_facts"] = ["not-an-object"]
    cases.append((wrong_item, "ordered business facts are missing"))
    invalid_sequence = deepcopy(stored.run_metadata)
    invalid_sequence["business_facts"][0]["operation_sequence"] = True
    cases.append((invalid_sequence, "operation sequence is invalid"))
    noncontiguous = deepcopy(stored.run_metadata)
    noncontiguous["business_facts"][0]["operation_sequence"] = 99
    cases.append((noncontiguous, "operation sequence is not contiguous"))
    malformed = deepcopy(stored.run_metadata)
    malformed["business_facts"][0]["transaction_count_before"] = True
    cases.append((malformed, "business fact is malformed"))
    unsupported = deepcopy(stored.run_metadata)
    unsupported["business_facts"][0]["kind"] = "unsupported"
    cases.append((unsupported, "unsupported ordered business fact"))

    for metadata, message in cases:
        with pytest.raises(ValidationError, match=message):
            replay_dividend_run(_replace_metadata(stored, metadata))


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("event_time", "event time mismatch"),
        ("marks", "mark state mismatch"),
        ("position_lots", "position lots mismatch"),
        ("transaction_sha256", "transaction sequence hash mismatch"),
        ("dividend_states", "lifecycle state mismatch"),
        ("account_snapshot", "account snapshot mismatch"),
        ("journal_sha256", "journal hash mismatch"),
    ],
)
def test_replay_rejects_tampered_final_comparison_targets(
    tmp_path: Path,
    target: str,
    message: str,
) -> None:
    stored = export_dividend_run(completed_ledger(), tmp_path / f"tamper-{target}")
    metadata = deepcopy(stored.run_metadata)
    final = metadata["final_facts"]
    if target == "event_time":
        final[target] = datetime(2099, 1, 1, tzinfo=UTC).isoformat()
    elif target in {"marks", "dividend_states"}:
        final[target] = []
    elif target == "position_lots":
        final[target] = {}
    elif target == "account_snapshot":
        final[target]["account_id"] = "tampered"
    else:
        final[target] = "0" * 64

    with pytest.raises(ValidationError, match=message):
        replay_dividend_run(_replace_metadata(stored, metadata))


def test_manifest_tampering_is_rejected_even_after_outer_hashes_are_recomputed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tampered"
    export_dividend_run(completed_ledger(), root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_metadata"]["initial_conditions"]["account_id"] = "tampered-account"
    unsigned_metadata = dict(manifest["run_metadata"])
    unsigned_metadata.pop("metadata_sha256")
    manifest["run_metadata"]["metadata_sha256"] = artifacts_module.hashlib.sha256(
        artifacts_module.canonical_bytes(unsigned_metadata)
    ).hexdigest()
    manifest["manifest_sha256"] = artifacts_module._manifest_hash(manifest)
    manifest_path.write_bytes(artifacts_module._canonical_manifest_bytes(manifest))

    loaded = load_stored_artifacts(root)
    with pytest.raises(ValidationError, match="initial transaction bytes"):
        replay_dividend_run(loaded)


@pytest.mark.parametrize(
    "failure",
    ["append", "commit", "writer", "seal", "candidate", "manifest", "final_load"],
)
def test_export_failures_poison_directory_preserve_source_and_allow_new_root(
    tmp_path: Path,
    monkeypatch,
    failure: str,
) -> None:
    account = completed_ledger()
    before = account.capture_state()
    failed_root = tmp_path / f"failed-{failure}"

    with monkeypatch.context() as scoped:
        if failure == "append":
            scoped.setattr(
                ArrowReplayArtifactSink,
                "append",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("append failure")),
            )
        elif failure == "commit":
            original = ArrowReplayArtifactSink._append_committed
            calls = {"count": 0}

            def fail_mid_commit(self, stream, payload):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise RuntimeError("commit failure")
                return original(self, stream, payload)

            scoped.setattr(ArrowReplayArtifactSink, "_append_committed", fail_mid_commit)
        elif failure == "writer":
            scoped.setattr(
                ArrowReplayArtifactSink,
                "_writer",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("writer failure")),
            )
        elif failure == "seal":
            scoped.setattr(
                ArrowReplayArtifactSink,
                "seal",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("seal failure")),
            )
        elif failure == "candidate":
            scoped.setattr(
                artifacts_module,
                "replay_dividend_run",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("candidate failure")),
            )
        elif failure == "manifest":
            original_write = artifacts_module._write_no_clobber

            def fail_manifest(path, body):
                if path.name == "manifest.json":
                    raise RuntimeError("manifest failure")
                return original_write(path, body)

            scoped.setattr(artifacts_module, "_write_no_clobber", fail_manifest)
        elif failure == "final_load":
            scoped.setattr(
                artifacts_module,
                "load_stored_artifacts",
                lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("close failure")),
            )

        with pytest.raises(RuntimeError):
            export_dividend_run(account, failed_root)

    assert account.capture_state() == before
    assert (failed_root / "FAILED.json").is_file()
    with pytest.raises(ValidationError, match="FAILED"):
        load_stored_artifacts(failed_root)
    with pytest.raises(FileExistsError):
        export_dividend_run(account, failed_root)

    new_root = tmp_path / f"recovered-{failure}"
    stored = export_dividend_run(account, new_root)
    assert replay_dividend_run(stored).ledger.journal_sha256 == account.journal_sha256


def test_manifest_1_1_sink_cannot_publish_without_candidate_validation(tmp_path: Path) -> None:
    sink = ArrowReplayArtifactSink(tmp_path / "unverified", manifest_schema_version="1.1.0")
    with pytest.raises(ValidationError, match="candidate replay"):
        sink.close({"kind": "unverified"})
    assert (tmp_path / "unverified" / "FAILED.json").is_file()
    with pytest.raises(RuntimeError, match="already closed"):
        sink.close({"kind": "retry"}, candidate_validator=lambda stored: None)
