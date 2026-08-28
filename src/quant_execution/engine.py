"""Fail-closed deterministic market-event replay engine."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from quant_data_kit import (
    BarEvent,
    BookDeltaEvent,
    BookSnapshotEvent,
    CorporateActionEvent,
    FundingRateEvent,
    MarketEvent,
    MarkPriceEvent,
    QuoteEvent,
    StatusEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError

from quant_execution.broker import DeterministicBroker
from quant_execution.contracts import (
    Fee,
    Fill,
    LedgerTransaction,
    Order,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    RunResult,
    TimeInForce,
)
from quant_execution.ledger import ExactAccountLedger
from quant_execution.protocols import MatchingModel, Strategy, StrategyContext
from quant_execution.rules import RuleBookRiskGate
from quant_execution.schemas import execution_payload

_EVENT_TYPES = (
    BarEvent,
    BookDeltaEvent,
    BookSnapshotEvent,
    CorporateActionEvent,
    FundingRateEvent,
    MarkPriceEvent,
    QuoteEvent,
    StatusEvent,
    TradeEvent,
)


class ReplayError(RuntimeError):
    """Replay stopped at the first unsafe or invalid input."""


@dataclass(frozen=True)
class RunArtifacts:
    market_events: tuple[MarketEvent, ...]
    orders: tuple[Order, ...]
    order_events: tuple[OrderEvent, ...]
    fills: tuple[Fill, ...]
    fees: tuple[Fee, ...]
    ledger_transactions: tuple[LedgerTransaction, ...]
    risk_events: tuple[str, ...]
    result: RunResult


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    ).encode()


def _hash(records: Sequence[object]) -> str:
    return hashlib.sha256(_canonical(records)).hexdigest()


def _event_sort_key(event: MarketEvent) -> tuple[object, ...]:
    return (
        event.available_at,
        event.event_time,
        event.source,
        event.instrument_id,
        event.session_id,
        -1 if event.sequence is None else event.sequence,
        event.event_id,
    )


class DeterministicRunEngine:
    """Research/backtest/paper-only replay; no adapter can transmit a live order."""

    sends_live_orders = False

    def __init__(
        self,
        *,
        run_id: str,
        account_id: str,
        strategy_id: str,
        strategy: Strategy,
        broker: DeterministicBroker,
        risk_gate: RuleBookRiskGate,
        matching_model: MatchingModel,
        ledger: ExactAccountLedger,
    ) -> None:
        if not run_id.strip() or not account_id.strip() or not strategy_id.strip():
            raise ValidationError("run_id, account_id and strategy_id are required")
        if account_id != ledger.account_id:
            raise ValidationError("engine account differs from ledger account")
        self.run_id = run_id
        self.account_id = account_id
        self.strategy_id = strategy_id
        self.strategy = strategy
        self.broker = broker
        self.risk_gate = risk_gate
        self.matching_model = matching_model
        self.ledger = ledger
        self.artifacts: RunArtifacts | None = None

    def replay(self, event_stream: Iterable[MarketEvent], seed: int) -> RunResult:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValidationError("seed must be a non-negative integer")
        events = self._validated_events(event_stream)
        checkpoint = self._capture_state()
        event: MarketEvent | None = None
        try:
            self._reset()
            context = StrategyContext(
                run_id=self.run_id,
                account_id=self.account_id,
                strategy_id=self.strategy_id,
                seed=seed,
                state={},
            )
            fills: list[Fill] = []
            fees: list[Fee] = []
            risk_events: list[str] = []
            seen_fill_ids: set[str] = set()
            for event in events:
                day_expiries = self.broker.expire_day_orders(event.trading_day, event.available_at)
                for expiry in day_expiries:
                    self.risk_gate.release_order(self._order(expiry.order_id))
                self.risk_gate.observe(event)
                account_snapshot = self.ledger.observe_market(
                    event,
                    create_snapshot=False,
                    trusted_unique=True,
                )
                if isinstance(event, CorporateActionEvent):
                    account_snapshot = self.ledger.apply(event, create_snapshot=False)
                elif isinstance(event, FundingRateEvent):
                    funding = self.ledger.funding_from_market(event)
                    if funding is not None:
                        account_snapshot = self.ledger.apply(funding, create_snapshot=False)

                if self.broker.open_orders:
                    for order in self.broker.open_orders:
                        if (
                            type(self.risk_gate).check_open_order
                            is RuleBookRiskGate.check_open_order
                        ):
                            decision = self.risk_gate.check_open_order_current(
                                order, event_time=event.available_at
                            )
                        else:
                            account_snapshot = account_snapshot or self.ledger.snapshot(
                                event.available_at
                            )
                            decision = self.risk_gate.check_open_order(
                                order,
                                account_snapshot,
                                event_time=event.available_at,
                            )
                        if not decision.accepted:
                            self.broker.expire(
                                order.order_id,
                                event_time=event.available_at,
                                reason=f"{decision.code}: {decision.message}",
                            )
                            self.risk_gate.release_order(order)
                            risk_events.append(
                                f"{order.order_id}:{decision.code}:{decision.message}"
                            )

                matched = tuple(self.matching_model.match(event, self.broker.open_orders))
                for fill in matched:
                    if fill.fill_id in seen_fill_ids:
                        raise ValidationError(f"duplicate fill_id: {fill.fill_id}")
                    order = self._order(fill.order_id)
                    fill_decision = self.risk_gate.check_fill(fill, order)
                    if not fill_decision.accepted:
                        self.broker.expire(
                            order.order_id,
                            event_time=event.available_at,
                            reason=f"{fill_decision.code}: {fill_decision.message}",
                        )
                        self.risk_gate.release_order(order)
                        risk_events.append(
                            f"{order.order_id}:{fill_decision.code}:{fill_decision.message}"
                        )
                        continue
                    self.broker.apply_fill(fill)
                    self.risk_gate.release_fill(fill, order)
                    account_snapshot = self.ledger.apply_with_trading_day(
                        fill,
                        trading_day=event.trading_day,
                        create_snapshot=False,
                    )
                    fee = self.risk_gate.fee_for(fill, order)
                    if fee is not None:
                        self.ledger.apply(fee, create_snapshot=False)
                        fees.append(fee)
                    account_snapshot = None
                    seen_fill_ids.add(fill.fill_id)
                    fills.append(fill)

                self._expire_immediate_orders(event)
                if type(self.risk_gate).runtime_check is RuleBookRiskGate.runtime_check:
                    runtime = self.risk_gate.runtime_check_current(event.available_at)
                else:
                    account_snapshot = account_snapshot or self.ledger.snapshot(event.available_at)
                    runtime = self.risk_gate.runtime_check(account_snapshot)
                if not runtime.accepted:
                    risk_events.append(f"{event.event_id}:{runtime.code}:{runtime.message}")
                    for order in self.broker.open_orders:
                        self.broker.expire(
                            order.order_id,
                            event_time=event.available_at,
                            reason=runtime.code,
                        )
                        self.risk_gate.release_order(order)

                intents = self._strategy_intents(context, event)
                for intent in intents:
                    if type(self.risk_gate).check is RuleBookRiskGate.check:
                        decision = self.risk_gate.check_current(
                            intent, event_time=event.available_at
                        )
                    else:
                        account_snapshot = account_snapshot or self.ledger.snapshot(
                            event.available_at
                        )
                        decision = self.risk_gate.check(intent, account_snapshot)
                    if decision.accepted:
                        order = self.broker.submit(intent)
                        self.broker.note_trading_day(order.order_id, event.trading_day)
                        if order.status in {
                            OrderStatus.ACCEPTED,
                            OrderStatus.PARTIALLY_FILLED,
                        }:
                            self.risk_gate.reserve(intent)
                    else:
                        self.broker.reject(intent, code=decision.code, message=decision.message)
                        risk_events.append(
                            f"{intent.idempotency_key}:{decision.code}:{decision.message}"
                        )
        except Exception as exc:
            self._restore_state(checkpoint)
            event_id = event.event_id if event is not None else "before-first-event"
            raise ReplayError(f"replay failed closed at {event_id}: {exc}") from exc

        try:
            order_payloads = [execution_payload(item) for item in self.broker.order_events]
            fill_payloads = [execution_payload(item) for item in fills]
            result = RunResult(
                run_id=self.run_id,
                seed=seed,
                event_count=len(events),
                order_count=len(self.broker.orders),
                fill_count=len(fills),
                event_sha256=_hash(order_payloads),
                fill_sha256=_hash(fill_payloads),
                ledger_sha256=self.ledger.journal_sha256,
            )
            self.artifacts = RunArtifacts(
                market_events=events,
                orders=self.broker.orders,
                order_events=self.broker.order_events,
                fills=tuple(fills),
                fees=tuple(fees),
                ledger_transactions=self.ledger.transactions,
                risk_events=tuple(risk_events),
                result=result,
            )
            return result
        except Exception as exc:
            self._restore_state(checkpoint)
            raise ReplayError(f"replay failed closed during finalization: {exc}") from exc

    def _reset(self) -> None:
        self.broker.reset()
        self.ledger.reset()
        reset = getattr(self.matching_model, "reset", None)
        if callable(reset):
            reset()
        self.risk_gate.reset()
        strategy_reset = getattr(self.strategy, "reset", None)
        if callable(strategy_reset):
            strategy_reset()
        self.artifacts = None

    @staticmethod
    def _capture_component(component: object) -> tuple[str, object]:
        try:
            capture = getattr(component, "capture_state", None)
            if callable(capture):
                return "explicit", capture()
            state = getattr(component, "__dict__", None)
            if state is None:
                raise ValidationError(
                    f"component {type(component).__name__} cannot provide atomic replay state"
                )
            return "dict", deepcopy(state)
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(
                f"component {type(component).__name__} state cannot be checkpointed"
            ) from exc

    @staticmethod
    def _restore_component(component: object, checkpoint: tuple[str, object]) -> None:
        mode, state = checkpoint
        if mode == "explicit":
            restore = getattr(component, "restore_state", None)
            if not callable(restore):
                raise ValidationError(f"component {type(component).__name__} lost restore_state")
            restore(state)
            return
        component_state = component.__dict__
        component_state.clear()
        component_state.update(deepcopy(state))

    def _capture_state(self) -> dict[str, object]:
        return {
            "broker": self._capture_component(self.broker),
            "ledger": self._capture_component(self.ledger),
            "risk_gate": self._capture_component(self.risk_gate),
            "matching_model": self._capture_component(self.matching_model),
            "strategy": self._capture_component(self.strategy),
            "artifacts": deepcopy(self.artifacts),
        }

    def _restore_state(self, checkpoint: dict[str, object]) -> None:
        self._restore_component(self.broker, checkpoint["broker"])
        self._restore_component(self.ledger, checkpoint["ledger"])
        self._restore_component(self.risk_gate, checkpoint["risk_gate"])
        self._restore_component(self.matching_model, checkpoint["matching_model"])
        self._restore_component(self.strategy, checkpoint["strategy"])
        self.artifacts = checkpoint["artifacts"]

    @staticmethod
    def _validated_events(event_stream: Iterable[MarketEvent]) -> tuple[MarketEvent, ...]:
        records = list(event_stream)
        if any(not isinstance(event, _EVENT_TYPES) for event in records):
            raise ValidationError("event_stream contains a non-MarketEvent value")
        event_ids: set[str] = set()
        for event in records:
            if event.event_id in event_ids:
                raise ValidationError(f"duplicate MarketEvent event_id: {event.event_id}")
            event_ids.add(event.event_id)
        return tuple(sorted(records, key=_event_sort_key))

    def _strategy_intents(
        self, context: StrategyContext, event: MarketEvent
    ) -> tuple[OrderIntent, ...]:
        values = self.strategy.on_event(context, event)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ValidationError("Strategy.on_event must return a Sequence[OrderIntent]")
        intents = tuple(values)
        if any(not isinstance(intent, OrderIntent) for intent in intents):
            raise ValidationError("Strategy.on_event returned a non-OrderIntent")
        for intent in intents:
            if intent.account_id != self.account_id or intent.strategy_id != self.strategy_id:
                raise ValidationError("strategy intent identity differs from engine context")
            if intent.created_at != event.available_at:
                raise ValidationError(
                    "strategy callback intent created_at must equal MarketEvent.available_at"
                )
        return tuple(sorted(intents, key=lambda item: item.idempotency_key))

    def _expire_immediate_orders(self, event: MarketEvent) -> None:
        eligible = getattr(self.matching_model, "eligible", None)
        if not callable(eligible):
            return
        for order in self.broker.open_orders:
            if order.intent.time_in_force not in {TimeInForce.IOC, TimeInForce.FOK}:
                continue
            if eligible(order, event):
                self.broker.expire(
                    order.order_id,
                    event_time=event.available_at,
                    reason=f"{order.intent.time_in_force.value.upper()} remainder expired",
                )
                self.risk_gate.release_order(order)

    def _order(self, order_id: str) -> Order:
        return self.broker.get_order(order_id)
