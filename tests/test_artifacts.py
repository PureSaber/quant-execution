from __future__ import annotations

import json
import queue
from datetime import date, timedelta

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import CorporateActionEvent, FundingRateEvent, MarkPriceEvent, StatusEvent
from quant_data_kit.exceptions import ValidationError
from test_engine import (
    FixtureStrategy,
    Signal,
    bar,
    engine_for,
    scenario_a_share,
    scenario_crypto,
    scenario_future,
)
from test_engine_edges import (
    LiquidatingGate,
    TransactionalBatchMatcher,
    spot_signal,
    transactional_engine,
)
from test_rules import FUTURE, PERP, SPOT, STOCK, specs

from quant_execution import (
    ArrowReplayArtifactSink,
    Fill,
    LiquidityRole,
    OrderIntent,
    OrderType,
    ReplayError,
    RiskDecision,
    Side,
    TimeInForce,
    load_stored_artifacts,
)
from quant_execution.artifacts import (
    _canonical_manifest_bytes,
    _manifest_hash,
    fee_bytes,
    fill_bytes,
    ledger_transaction_bytes,
    order_bytes,
    order_event_bytes,
    settlement_bytes,
)
from quant_execution.broker import DeterministicBroker
from quant_execution.engine import _event_sort_key
from quant_execution.ledger import ExactAccountLedger
from quant_execution.matching import BarMatchingModel
from quant_execution.rules import RuleBookRiskGate


@pytest.mark.parametrize(
    "factory",
    (scenario_a_share, scenario_future, scenario_crypto),
    ids=("a-share", "future", "crypto-spot-perpetual-funding"),
)
def test_streamed_replay_is_byte_identical_to_memory_reference(factory, tmp_path) -> None:
    reference, reference_events = factory()
    expected = reference.replay(reference_events, 42)
    assert reference.artifacts is not None
    expected_artifacts = reference.artifacts
    expected_nav = reference.ledger.snapshot().nav

    streamed, streamed_events = factory()
    sink = ArrowReplayArtifactSink(tmp_path / "run", batch_size=2, queue_batches=1)
    actual = streamed.replay_to_sink(
        sorted(streamed_events, key=_event_sort_key),
        42,
        sink,
    )
    stored = streamed.stored_artifacts
    assert stored is not None
    assert actual == expected
    assert streamed.ledger.snapshot().nav == expected_nav
    assert streamed.ledger.transaction_count == stored.counts["ledger_transactions"]
    assert streamed.ledger.journal_sha256 == actual.ledger_sha256
    assert tuple(stored.iter_payload_bytes("orders")) == tuple(
        order_bytes(value) for value in expected_artifacts.orders
    )
    assert tuple(stored.iter_payload_bytes("order_events")) == tuple(
        order_event_bytes(value) for value in expected_artifacts.order_events
    )
    assert tuple(stored.iter_payload_bytes("fills")) == tuple(
        fill_bytes(value) for value in expected_artifacts.fills
    )
    assert tuple(stored.iter_payload_bytes("fees")) == tuple(
        fee_bytes(value) for value in expected_artifacts.fees
    )
    assert tuple(stored.iter_payload_bytes("settlements")) == tuple(
        settlement_bytes(value) for value in expected_artifacts.settlements
    )
    assert tuple(stored.iter_payload_bytes("ledger_transactions")) == tuple(
        ledger_transaction_bytes(value) for value in expected_artifacts.ledger_transactions
    )
    assert tuple(stored.iter_json("risk_events")) == expected_artifacts.risk_events
    verified = load_stored_artifacts(stored.root)
    assert verified.manifest_sha256 == stored.manifest_sha256
    manifest = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["run_metadata"]["result_sha256"] == expected.result_sha256


def test_streaming_broker_terminal_order_remains_idempotently_readable(tmp_path) -> None:
    broker = DeterministicBroker()
    sink = ArrowReplayArtifactSink(tmp_path / "broker", batch_size=1)
    broker.start_artifact_stream(sink)
    intent = OrderIntent(
        idempotency_key="cancel-me",
        account_id="account",
        strategy_id="strategy",
        instrument_id="crypto:test:BTCUSDT",
        side=Side.BUY,
        quantity=fp("1.000", 3),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp("100"),
    )
    accepted = broker.submit(intent)
    cancelled = broker.cancel(
        accepted.order_id,
        idempotency_key="cancel-request",
        created_at=T0 + timedelta(seconds=1),
    )
    assert broker.submit(intent).order_id == accepted.order_id
    assert (
        broker.cancel(
            accepted.order_id,
            idempotency_key="cancel-request",
            created_at=T0 + timedelta(seconds=1),
        )
        == cancelled
    )
    assert broker.get_order(accepted.order_id).status.value == "cancelled"
    broker.finish_artifact_stream()
    stored = sink.close({"run_id": "broker-only"})
    assert stored.counts["orders"] == 1
    assert stored.counts["order_events"] == 2


def test_streaming_broker_fill_compaction_and_lifecycle_guards(tmp_path) -> None:
    broker = DeterministicBroker()
    sink = ArrowReplayArtifactSink(tmp_path / "broker-fill", batch_size=1)
    with pytest.raises(ValidationError, match="provide append"):
        broker.start_artifact_stream(object())
    broker.start_artifact_stream(sink)
    with pytest.raises(ValidationError, match="immediately after reset"):
        broker.start_artifact_stream(sink)
    intent = OrderIntent(
        idempotency_key="fill-me",
        account_id="account",
        strategy_id="strategy",
        instrument_id="crypto:test:BTCUSDT",
        side=Side.BUY,
        quantity=fp("1.000", 3),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp("100"),
    )
    order = broker.submit(intent)
    fill = Fill(
        fill_id="fill-compact",
        order_id=order.order_id,
        account_id="account",
        strategy_id="strategy",
        instrument_id=intent.instrument_id,
        side=Side.BUY,
        quantity=intent.quantity,
        price=fp("100"),
        event_time=T0 + timedelta(seconds=1),
        liquidity_role=LiquidityRole.TAKER,
    )
    event = broker.apply_fill(fill)
    assert broker.apply_fill(fill) == event
    conflicting = Fill(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        account_id=fill.account_id,
        strategy_id=fill.strategy_id,
        instrument_id=fill.instrument_id,
        side=fill.side,
        quantity=fill.quantity,
        price=fp("101"),
        event_time=fill.event_time,
        liquidity_role=fill.liquidity_role,
    )
    with pytest.raises(ValidationError, match="different fill content"):
        broker.apply_fill(conflicting)
    terminal_payload = broker._orders[order.order_id]
    assert isinstance(terminal_payload, bytes)
    broker._open_order_ids.add(order.order_id)
    with pytest.raises(TypeError, match="terminal order"):
        broker._live_order(order.order_id)
    broker._open_order_ids.clear()
    broker.finish_artifact_stream()
    with pytest.raises(ValidationError, match="stream is closed"):
        broker.submit(intent)
    with pytest.raises(ValidationError, match="stream is closed"):
        broker.apply_fill(fill)
    with pytest.raises(ValidationError, match="stream is closed"):
        broker.note_trading_day(order.order_id, T0.date())
    with pytest.raises(ValidationError, match="stream is closed"):
        broker.expire_day_orders(T0.date(), T0 + timedelta(days=1))
    with pytest.raises(ValidationError, match="stream is closed"):
        broker.start_artifact_stream(sink)
    broker.finish_artifact_stream()
    sink.close({"run_id": "broker-fill"})


def test_streaming_replay_fails_closed_for_unsorted_and_duplicate_inputs(tmp_path) -> None:
    engine, events = scenario_a_share()
    with pytest.raises(ReplayError, match="not deterministically sorted"):
        engine.replay_to_sink(events, 42, ArrowReplayArtifactSink(tmp_path / "unsorted"))
    assert json.loads((tmp_path / "unsorted" / "FAILED.json").read_text())["complete"] is False

    engine, events = scenario_a_share()
    ordered = sorted(events, key=_event_sort_key)
    with pytest.raises(ReplayError, match="duplicate MarketEvent event_id"):
        engine.replay_to_sink(
            (ordered[0], ordered[0]),
            42,
            ArrowReplayArtifactSink(tmp_path / "duplicate"),
        )


def test_sink_validation_transaction_and_unknown_stream_branches(tmp_path) -> None:
    with pytest.raises(ValidationError, match="batch_size"):
        ArrowReplayArtifactSink(tmp_path / "bad-batch", batch_size=0)
    with pytest.raises(ValidationError, match="queue_batches"):
        ArrowReplayArtifactSink(tmp_path / "bad-queue", queue_batches=0)

    sink = ArrowReplayArtifactSink(tmp_path / "transactions", batch_size=1)
    with pytest.raises(ValidationError, match="unknown artifact stream"):
        sink.append("unknown", b"{}")
    with pytest.raises(ValidationError, match="canonical bytes"):
        sink.append("fills", "not-bytes")
    sink.begin()
    sink.append("risk_events", b'"rolled-back"')
    with pytest.raises(RuntimeError, match="nested"):
        sink.begin()
    sink.rollback()
    with pytest.raises(RuntimeError, match="no artifact transaction"):
        sink.rollback()
    sink.begin()
    sink.append("risk_events", b'"committed"')
    sink.commit()
    stored = sink.close({"run_id": "transaction-test"})
    assert tuple(stored.iter_json("risk_events")) == ("committed",)
    assert tuple(stored.iter_json("settlements")) == ()
    with pytest.raises(ValidationError, match="unknown artifact stream"):
        tuple(stored.iter_json("unknown"))
    with pytest.raises(RuntimeError, match="already closed"):
        sink.close({})


def test_streaming_input_validation_empty_and_sink_failure_branches(tmp_path, monkeypatch) -> None:
    for index, seed in enumerate((True, "1", -1)):
        engine, events = scenario_a_share()
        sink = ArrowReplayArtifactSink(tmp_path / f"seed-{index}")
        with pytest.raises(ValidationError, match="seed"):
            engine.replay_to_sink(events, seed, sink)
        sink.abort()
    engine, events = scenario_a_share()
    with pytest.raises(ValidationError, match="ArrowReplayArtifactSink"):
        engine.replay_to_sink(events, 1, object())

    engine, _ = scenario_a_share()
    result = engine.replay_to_sink((), 1, ArrowReplayArtifactSink(tmp_path / "empty"))
    assert result.event_count == result.order_count == result.fill_count == 0

    engine, _ = scenario_a_share()
    with pytest.raises(ReplayError, match="non-MarketEvent"):
        engine.replay_to_sink((object(),), 1, ArrowReplayArtifactSink(tmp_path / "bad-first"))
    engine, events = scenario_a_share()
    ordered = sorted(events, key=_event_sort_key)
    with pytest.raises(ReplayError, match="non-MarketEvent"):
        engine.replay_to_sink(
            (ordered[0], object()),
            1,
            ArrowReplayArtifactSink(tmp_path / "bad-later"),
        )

    failing = ArrowReplayArtifactSink(tmp_path / "writer-failure", batch_size=1)

    def fail_writer(stream):
        del stream
        raise OSError("injected Arrow writer failure")

    monkeypatch.setattr(failing, "_writer", fail_writer)
    failing.append("fills", b"{}")
    with pytest.raises(RuntimeError, match="artifact writer failed"):
        failing.seal()
    failing.abort()


def test_failed_second_streaming_replay_restores_completed_artifact_handle_and_ledger(
    tmp_path,
) -> None:
    engine, events = scenario_a_share()
    ordered = sorted(events, key=_event_sort_key)
    first = engine.replay_to_sink(ordered, 42, ArrowReplayArtifactSink(tmp_path / "completed"))
    completed = engine.stored_artifacts
    assert completed is not None
    completed_count = engine.ledger.transaction_count
    completed_hash = engine.ledger.journal_sha256

    with pytest.raises(ReplayError, match="non-MarketEvent"):
        engine.replay_to_sink(
            (ordered[0], object()),
            42,
            ArrowReplayArtifactSink(tmp_path / "failed-second"),
        )

    assert engine.stored_artifacts == completed
    assert engine.ledger.transaction_count == completed_count
    assert engine.ledger.journal_sha256 == completed_hash == first.ledger_sha256
    assert (tmp_path / "failed-second" / "FAILED.json").is_file()


def test_sink_defensive_state_and_queue_full_branches(tmp_path, monkeypatch) -> None:
    from quant_execution.artifacts import _SequenceDigest

    digest = _SequenceDigest()
    digest.append(b"{}")
    digest.close()
    with pytest.raises(RuntimeError, match="already closed"):
        digest.append(b"{}")

    sink = ArrowReplayArtifactSink(tmp_path / "states")
    with pytest.raises(RuntimeError, match="no artifact transaction"):
        sink.commit()
    sink.begin()
    with pytest.raises(RuntimeError, match="active artifact transaction"):
        sink.seal()
    sink.rollback()
    with pytest.raises(ValidationError, match="unknown artifact stream"):
        sink.logical_sha256("unknown")
    assert tuple(sink._iter_payload_bytes("fills")) == ()

    original_put = sink._queue.put
    calls = 0

    def full_once(item, timeout=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise queue.Full
        return original_put(item, timeout=timeout)

    monkeypatch.setattr(sink._queue, "put", full_once)
    sink.append("fills", b"{}")
    stored = sink.close({"run_id": "queue-full-once"})
    assert stored.counts["fills"] == 1
    with pytest.raises(RuntimeError, match="no longer accepts"):
        sink.append("fills", b"{}")
    with pytest.raises(RuntimeError, match="no longer accepts"):
        sink.begin()
    sink.abort()

    invalid_manifest = ArrowReplayArtifactSink(tmp_path / "invalid-manifest")
    with pytest.raises(TypeError):
        invalid_manifest.close({"not_json": object()})
    assert (tmp_path / "invalid-manifest" / "FAILED.json").exists()


def test_stored_artifact_loader_rejects_physical_and_manifest_tampering(tmp_path) -> None:
    sink = ArrowReplayArtifactSink(tmp_path / "physical", batch_size=1)
    sink.append("fills", b"{}")
    stored = sink.close({"run_id": "physical"})
    assert load_stored_artifacts(stored.root).counts["fills"] == 1
    fill_path = stored.root / "fills.arrow"
    fill_path.write_bytes(fill_path.read_bytes() + b"tampered")
    with pytest.raises(ValidationError, match="missing or changed size"):
        load_stored_artifacts(stored.root)

    sink = ArrowReplayArtifactSink(tmp_path / "manifest", batch_size=1)
    sink.append("fills", b"{}")
    stored = sink.close({"run_id": "manifest"})
    payload = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
    stored.manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(ValidationError, match="not canonical"):
        load_stored_artifacts(stored.root)


def test_stored_artifact_loader_rejects_manifest_contract_mutations(tmp_path) -> None:
    def artifact(case: str, *, with_fill: bool = False):
        sink = ArrowReplayArtifactSink(tmp_path / case, batch_size=1)
        if with_fill:
            sink.append("fills", b"{}")
        return sink.close({"run_id": case})

    def rewrite(stored, payload, *, rehash: bool = True) -> None:
        if rehash:
            payload["manifest_sha256"] = _manifest_hash(payload)
        stored.manifest_path.write_bytes(_canonical_manifest_bytes(payload))

    unreadable = artifact("unreadable")
    unreadable.manifest_path.write_bytes(b"{")
    with pytest.raises(ValidationError, match="manifest is unreadable"):
        load_stored_artifacts(unreadable.root)

    non_object = artifact("non-object")
    non_object.manifest_path.write_bytes(b"[]\n")
    with pytest.raises(ValidationError, match="root must be an object"):
        load_stored_artifacts(non_object.root)

    top_level_cases = (
        ("fields", "unexpected", True, "fields changed"),
        ("metadata", "run_metadata", {}, "no run metadata"),
        ("schema", "schema_version", "2.0.0", "schema version"),
        ("format", "artifact_format", "unknown", "format is unsupported"),
        ("incomplete", "complete", False, "run is not complete"),
        ("counts-shape", "counts", {}, "counts changed shape"),
        ("logical-shape", "logical_sha256", {}, "logical hashes changed shape"),
        ("files-type", "files", [], "files must be an object"),
    )
    for case, field, value, message in top_level_cases:
        stored = artifact(case)
        payload = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
        payload[field] = value
        rewrite(stored, payload)
        with pytest.raises(ValidationError, match=message):
            load_stored_artifacts(stored.root)

    invalid_hash = artifact("invalid-hash")
    payload = json.loads(invalid_hash.manifest_path.read_text(encoding="utf-8"))
    payload["manifest_sha256"] = "not-a-sha"
    rewrite(invalid_hash, payload, rehash=False)
    with pytest.raises(ValidationError, match="manifest hash mismatch"):
        load_stored_artifacts(invalid_hash.root)

    empty_stream_cases = (
        ("count-bool", "counts", True, "count is invalid"),
        ("count-negative", "counts", -1, "count is invalid"),
        ("logical-format", "logical_sha256", "bad", "logical hash is invalid"),
        ("unexpected-file", "files", {"path": "fees.arrow"}, "unexpectedly has a file"),
        ("empty-hash", "logical_sha256", "0" * 64, "empty artifact logical hash mismatch"),
    )
    for case, section, value, message in empty_stream_cases:
        stored = artifact(case)
        payload = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
        payload[section]["fees"] = value
        rewrite(stored, payload)
        with pytest.raises(ValidationError, match=message):
            load_stored_artifacts(stored.root)

    file_cases = (
        ("file-shape", "files", {"path": "fills.arrow"}, "metadata changed shape"),
        (
            "file-path",
            "files",
            {"bytes": 1, "path": "wrong.arrow", "sha256": "0" * 64},
            "file path is invalid",
        ),
        ("file-bytes", "bytes", True, "file metadata is invalid"),
        ("file-sha", "sha256", "bad", "file metadata is invalid"),
    )
    for case, field, value, message in file_cases:
        stored = artifact(case, with_fill=True)
        payload = json.loads(stored.manifest_path.read_text(encoding="utf-8"))
        if field == "files":
            payload["files"]["fills"] = value
        else:
            payload["files"]["fills"][field] = value
        rewrite(stored, payload)
        with pytest.raises(ValidationError, match=message):
            load_stored_artifacts(stored.root)

    unknown_stream = artifact("unknown-stream")
    payload = json.loads(unknown_stream.manifest_path.read_text(encoding="utf-8"))
    payload["files"]["unknown"] = {"bytes": 1, "path": "unknown.arrow", "sha256": "0" * 64}
    rewrite(unknown_stream, payload)
    with pytest.raises(ValidationError, match="unknown streams"):
        load_stored_artifacts(unknown_stream.root)


def test_artifact_manifest_publish_is_no_clobber(tmp_path) -> None:
    sink = ArrowReplayArtifactSink(tmp_path / "no-clobber", batch_size=1)
    sink.append("fills", b"{}")
    manifest_path = sink.root / "manifest.json"
    manifest_path.write_text("pre-existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sink.close({"run_id": "no-clobber"})
    assert manifest_path.read_text(encoding="utf-8") == "pre-existing"
    assert (sink.root / "FAILED.json").is_file()


def test_streaming_day_expiry_rejection_latency_and_liquidation_paths(tmp_path) -> None:
    registry = {STOCK: specs()[STOCK]}

    day_strategy = FixtureStrategy(
        {"day-signal": [Signal(STOCK, Side.BUY, fp("100"), fp("8.5"), tif=TimeInForce.DAY)]}
    )
    day_engine = engine_for(
        run_id="stream-day-expiry",
        registry=registry,
        initial_cash={"CNY": fp("100000")},
        base_currency="CNY",
        strategy=day_strategy,
    )
    day_events = [
        bar("day-signal", STOCK, 60, "10"),
        bar("next-day", STOCK, 120, "10"),
    ]
    object.__setattr__(day_events[1], "trading_day", date(2026, 1, 3))
    day_engine.replay_to_sink(
        day_events,
        1,
        ArrowReplayArtifactSink(tmp_path / "day-expiry"),
    )
    assert day_engine.stored_artifacts is not None
    assert day_engine.stored_artifacts.counts["order_events"] == 2

    reject_strategy = FixtureStrategy({"reject": [Signal(STOCK, Side.BUY, fp("100"), fp("10"))]})
    reject_engine = engine_for(
        run_id="stream-reject",
        registry=registry,
        initial_cash={"CNY": fp("10")},
        base_currency="CNY",
        strategy=reject_strategy,
    )
    rejected = reject_engine.replay_to_sink(
        [bar("reject", STOCK, 60, "10")],
        1,
        ArrowReplayArtifactSink(tmp_path / "reject"),
    )
    assert rejected.order_count == 1 and rejected.fill_count == 0

    latency_strategy = FixtureStrategy({"latency": [Signal(STOCK, Side.BUY, fp("100"), fp("10"))]})
    latency_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
    )
    from quant_execution.engine import DeterministicRunEngine

    latency_engine = DeterministicRunEngine(
        run_id="stream-latency",
        account_id="account",
        strategy_id="strategy",
        strategy=latency_strategy,
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=latency_ledger),
        matching_model=BarMatchingModel(registry, latency=timedelta(hours=1)),
        ledger=latency_ledger,
    )
    latency_result = latency_engine.replay_to_sink(
        [bar("latency", STOCK, 60, "10"), bar("too-soon", STOCK, 120, "10")],
        1,
        ArrowReplayArtifactSink(tmp_path / "latency"),
    )
    assert latency_result.fill_count == 0

    liquidation_strategy = FixtureStrategy(
        {"liquidation-signal": [Signal(STOCK, Side.BUY, fp("100"), fp("8.5"))]}
    )
    liquidation_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
    )
    liquidation_engine = DeterministicRunEngine(
        run_id="stream-liquidation",
        account_id="account",
        strategy_id="strategy",
        strategy=liquidation_strategy,
        broker=DeterministicBroker(),
        risk_gate=LiquidatingGate(instruments=registry, ledger=liquidation_ledger),
        matching_model=BarMatchingModel(registry),
        ledger=liquidation_ledger,
    )
    liquidation_engine.replay_to_sink(
        [
            bar("liquidation-signal", STOCK, 60, "10"),
            bar("liquidation-boundary", STOCK, 120, "10"),
        ],
        1,
        ArrowReplayArtifactSink(tmp_path / "liquidation"),
    )
    assert liquidation_engine.stored_artifacts is not None
    assert liquidation_engine.stored_artifacts.counts["risk_events"] == 1

    suspended_engine = engine_for(
        run_id="stream-suspended",
        registry=registry,
        initial_cash={"CNY": fp("100000")},
        base_currency="CNY",
        strategy=FixtureStrategy(
            {"suspended-signal": [Signal(STOCK, Side.BUY, fp("100"), fp("9"))]}
        ),
    )
    halted = StatusEvent(
        event_id="halt",
        instrument_id=STOCK,
        event_time=T0 + timedelta(seconds=120),
        received_at=T0 + timedelta(seconds=120),
        available_at=T0 + timedelta(seconds=120),
        source="fixture",
        trading_day=T0.date(),
        session_id=f"session:{T0.date().isoformat()}",
        sequence=120,
        status="suspended",
        reason="fixture",
    )
    suspended = suspended_engine.replay_to_sink(
        [
            bar("suspended-signal", STOCK, 60, "10"),
            halted,
            bar("would-fill", STOCK, 180, "9"),
        ],
        1,
        ArrowReplayArtifactSink(tmp_path / "suspended"),
    )
    assert suspended.fill_count == 0


@pytest.mark.parametrize(
    ("prices", "expected_fills"),
    (({0: ("100",), 1: ("101",)}, 2), ({0: ("100",), 1: ("200",)}, 1)),
)
def test_streaming_multi_fill_transaction_commit_and_rollback(
    prices, expected_fills, tmp_path
) -> None:
    strategy = FixtureStrategy({"signal": [spot_signal(), spot_signal()]})
    matcher = TransactionalBatchMatcher(prices)
    engine = transactional_engine(run_id="stream-multi", strategy=strategy, matcher=matcher)
    result = engine.replay_to_sink(
        [
            bar("signal", SPOT, 60, "100", volume="10.000"),
            bar("match", SPOT, 120, "100", volume="10.000"),
        ],
        4,
        ArrowReplayArtifactSink(tmp_path / f"multi-{expected_fills}", batch_size=1),
    )
    assert result.fill_count == expected_fills
    assert engine.stored_artifacts is not None
    assert engine.stored_artifacts.counts["fills"] == expected_fills


def test_streaming_ledger_compact_idempotency_and_stream_guards(tmp_path) -> None:
    spot = specs()[SPOT]
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={spot.instrument_id: spot},
        initial_cash={"USDT": fp("1000")},
    )
    sink = ArrowReplayArtifactSink(tmp_path / "ledger", batch_size=1)
    with pytest.raises(ValidationError, match="provide append"):
        ledger.start_artifact_stream(object())
    ledger.start_artifact_stream(sink)
    with pytest.raises(ValidationError, match="already active"):
        ledger.start_artifact_stream(sink)
    with pytest.raises(ValidationError, match="unavailable until artifact finalization"):
        _ = ledger.journal_sha256
    fill = Fill(
        fill_id="ledger-stream-fill",
        order_id="external",
        account_id="account",
        strategy_id="strategy",
        instrument_id=spot.instrument_id,
        side=Side.BUY,
        quantity=fp("1.000", 3),
        price=fp("100"),
        event_time=T0,
        liquidity_role=LiquidityRole.TAKER,
    )
    ledger.apply_with_trading_day(fill, trading_day=T0.date(), create_snapshot=False)
    ledger.apply_with_trading_day(fill, trading_day=T0.date(), create_snapshot=False)
    conflicting = Fill(
        fill_id=fill.fill_id,
        order_id=fill.order_id,
        account_id=fill.account_id,
        strategy_id=fill.strategy_id,
        instrument_id=fill.instrument_id,
        side=fill.side,
        quantity=fill.quantity,
        price=fp("101"),
        event_time=fill.event_time,
        liquidity_role=fill.liquidity_role,
    )
    with pytest.raises(ValidationError, match="different content"):
        ledger.apply_with_trading_day(
            conflicting,
            trading_day=T0.date(),
            create_snapshot=False,
        )
    with pytest.raises(ValidationError, match="requires a trading_day"):
        ledger._apply_replay_event(fill)
    with pytest.raises(ValidationError, match="only valid for fill"):
        ledger._apply_replay_event(
            CorporateActionEvent(
                event_id="action-invalid-day",
                instrument_id=spot.instrument_id,
                event_time=T0,
                received_at=T0,
                available_at=T0,
                source="fixture",
                trading_day=T0.date(),
                session_id="session",
                sequence=1,
                action_type="cash_dividend",
                effective_date=T0.date(),
                cash_amount=fp("1"),
                currency="USDT",
            ),
            trading_day=T0.date(),
        )
    assert ledger.transaction_count == 2
    with pytest.raises(ValidationError, match="lowercase SHA-256"):
        ledger.finish_artifact_stream(journal_sha256="bad")
    journal_sha256 = ledger.finish_artifact_stream()
    assert journal_sha256 == ledger.journal_sha256
    assert ledger.transaction_count == 2
    assert ledger.finish_artifact_stream() == journal_sha256
    sealed_snapshot = ledger.snapshot()
    with pytest.raises(ValidationError, match="stream is closed"):
        ledger.apply_with_trading_day(fill, trading_day=T0.date(), create_snapshot=False)
    with pytest.raises(ValidationError, match="stream is closed"):
        ledger.mark(
            MarkPriceEvent(
                **event_fields("sealed-mark", spot.instrument_id, seconds=1),
                price=fp("101"),
            )
        )
    with pytest.raises(ValidationError, match="stream is closed"):
        ledger.set_fx_rate("USD", fp("1"), event_time=T0 + timedelta(seconds=1))
    with pytest.raises(ValidationError, match="stream is closed"):
        ledger.start_artifact_stream(sink)
    assert ledger.snapshot() == sealed_snapshot
    assert ledger.transaction_count == 2
    assert ledger.journal_sha256 == journal_sha256
    sink.close({"run_id": "ledger-compact"})


def test_aborted_artifact_components_require_reset_before_reuse(tmp_path) -> None:
    intent = OrderIntent(
        idempotency_key="after-abort",
        account_id="account",
        strategy_id="strategy",
        instrument_id=SPOT,
        side=Side.BUY,
        quantity=fp("1.000", 3),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp("100"),
    )
    broker = DeterministicBroker()
    broker_sink = ArrowReplayArtifactSink(tmp_path / "aborted-broker", batch_size=1)
    broker.start_artifact_stream(broker_sink)
    broker.abort_artifact_stream()
    with pytest.raises(ValidationError, match="stream is closed"):
        broker.submit(intent)
    broker.reset()
    assert broker.submit(intent).intent == intent
    broker_sink.abort()

    spot = specs()[SPOT]
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={spot.instrument_id: spot},
        initial_cash={"USDT": fp("1000")},
    )
    ledger_sink = ArrowReplayArtifactSink(tmp_path / "aborted-ledger", batch_size=1)
    ledger.start_artifact_stream(ledger_sink)
    ledger.abort_artifact_stream()
    assert ledger.transaction_count == 1
    with pytest.raises(ValidationError, match="unavailable after artifact abort"):
        _ = ledger.journal_sha256
    with pytest.raises(ValidationError, match="stream is closed"):
        ledger.set_fx_rate("USD", fp("1"), event_time=T0)
    ledger.reset()
    ledger.set_fx_rate("USD", fp("1"), event_time=T0)
    ledger_sink.abort()


def test_streaming_corporate_funding_settlement_and_custom_gate_paths(tmp_path) -> None:
    registry = {STOCK: specs()[STOCK]}
    corporate = engine_for(
        run_id="stream-corporate",
        registry=registry,
        initial_cash={"CNY": fp("100000")},
        base_currency="CNY",
        strategy=FixtureStrategy(
            {"corporate-signal": [Signal(STOCK, Side.BUY, fp("100"), fp("10"))]}
        ),
    )
    action = CorporateActionEvent(
        event_id="split",
        instrument_id=STOCK,
        event_time=T0 + timedelta(seconds=180),
        received_at=T0 + timedelta(seconds=180),
        available_at=T0 + timedelta(seconds=180),
        source="fixture",
        trading_day=T0.date(),
        session_id=f"session:{T0.date().isoformat()}",
        sequence=180,
        action_type="split",
        effective_date=T0.date(),
        ratio=fp("2"),
    )
    corporate.replay_to_sink(
        [
            bar("corporate-signal", STOCK, 60, "10"),
            bar("corporate-fill", STOCK, 120, "10"),
            action,
        ],
        1,
        ArrowReplayArtifactSink(tmp_path / "corporate"),
    )
    assert corporate.ledger.snapshot().positions[STOCK].to_decimal() == fp("200").to_decimal()

    perp_registry = {PERP: specs()[PERP]}
    no_funding = engine_for(
        run_id="stream-no-funding",
        registry=perp_registry,
        initial_cash={"USDT": fp("1000")},
        base_currency="USDT",
        strategy=FixtureStrategy({}),
    )
    funding = FundingRateEvent(
        event_id="no-position-funding",
        instrument_id=PERP,
        event_time=T0 + timedelta(seconds=60),
        received_at=T0 + timedelta(seconds=60),
        available_at=T0 + timedelta(seconds=60),
        source="fixture",
        trading_day=T0.date(),
        session_id=f"session:{T0.date().isoformat()}",
        sequence=60,
        rate=0.001,
        interval_start=T0,
        interval_end=T0 + timedelta(hours=8),
    )
    no_funding.replay_to_sink([funding], 1, ArrowReplayArtifactSink(tmp_path / "no-funding"))

    future_registry = {FUTURE: specs()[FUTURE]}
    settlement_engine = engine_for(
        run_id="stream-settlement",
        registry=future_registry,
        initial_cash={"CNY": fp("1000000")},
        base_currency="CNY",
        strategy=FixtureStrategy(
            {"settlement-signal": [Signal(FUTURE, Side.BUY, fp("1"), fp("4000"))]}
        ),
    )
    status = StatusEvent(
        event_id="settlement-close",
        instrument_id=FUTURE,
        event_time=T0 + timedelta(seconds=240),
        received_at=T0 + timedelta(seconds=240),
        available_at=T0 + timedelta(seconds=240),
        source="fixture",
        trading_day=T0.date(),
        session_id=f"session:{T0.date().isoformat()}",
        sequence=240,
        status="daily_settlement",
        reason="fixture close",
    )
    settlement_engine.replay_to_sink(
        [
            bar("settlement-signal", FUTURE, 60, "4000"),
            bar("settlement-fill", FUTURE, 120, "4000"),
            bar("settlement-mark", FUTURE, 180, "4010"),
            status,
        ],
        1,
        ArrowReplayArtifactSink(tmp_path / "settlement"),
    )
    assert settlement_engine.stored_artifacts is not None
    assert settlement_engine.stored_artifacts.counts["settlements"] == 1

    class DelegatingGate(RuleBookRiskGate):
        def check(self, order_intent, account_snapshot):
            return super().check(order_intent, account_snapshot)

        def check_open_order(self, order, account_snapshot, *, event_time):
            return super().check_open_order(order, account_snapshot, event_time=event_time)

        def runtime_check(self, account_snapshot):
            return RiskDecision(True, "OK")

    custom_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
    )
    from quant_execution.engine import DeterministicRunEngine

    custom = DeterministicRunEngine(
        run_id="stream-custom-gate",
        account_id="account",
        strategy_id="strategy",
        strategy=FixtureStrategy(
            {"custom-signal": [Signal(STOCK, Side.BUY, fp("100"), fp("8.5"))]}
        ),
        broker=DeterministicBroker(),
        risk_gate=DelegatingGate(instruments=registry, ledger=custom_ledger),
        matching_model=BarMatchingModel(registry),
        ledger=custom_ledger,
    )
    custom.replay_to_sink(
        [bar("custom-signal", STOCK, 60, "10"), bar("custom-next", STOCK, 120, "10")],
        1,
        ArrowReplayArtifactSink(tmp_path / "custom-gate"),
    )
