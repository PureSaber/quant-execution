"""Public interfaces frozen for execution implementations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from quant_data_kit import MarketEvent

from quant_execution.contracts import (
    AccountSnapshot,
    Fill,
    LedgerEvent,
    Order,
    OrderEvent,
    OrderIntent,
    RiskDecision,
    RunResult,
)


@dataclass(frozen=True, kw_only=True)
class StrategyContext:
    run_id: str
    account_id: str
    strategy_id: str
    seed: int
    state: Any = None


class Strategy(Protocol):
    def on_event(self, context: StrategyContext, event: MarketEvent) -> Sequence[OrderIntent]: ...


class BrokerSimulator(Protocol):
    def submit(self, order_intent: OrderIntent) -> Order: ...

    def cancel(
        self,
        order_id: str,
        *,
        idempotency_key: str,
        created_at: datetime,
    ) -> OrderEvent: ...


class RiskGate(Protocol):
    def check(
        self, order_intent: OrderIntent, account_snapshot: AccountSnapshot
    ) -> RiskDecision: ...


class MatchingModel(Protocol):
    def match(self, market_event: MarketEvent, open_orders: Sequence[Order]) -> Sequence[Fill]: ...


class AccountLedger(Protocol):
    def apply(self, event: LedgerEvent) -> AccountSnapshot: ...


class RunEngine(Protocol):
    def replay(self, event_stream: Iterable[MarketEvent], seed: int) -> RunResult: ...
