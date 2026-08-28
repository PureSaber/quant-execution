"""Reproducible local M3a replay throughput and memory gate."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_data_kit import AssetClass, BarEvent, FixedPoint, InstrumentSpec

from quant_execution import OrderIntent, OrderType, Side, TimeInForce
from quant_execution.broker import DeterministicBroker
from quant_execution.engine import DeterministicRunEngine
from quant_execution.ledger import ExactAccountLedger
from quant_execution.matching import BarMatchingModel
from quant_execution.rules import RuleBookRiskGate

UTC = timezone.utc
START = datetime(2026, 1, 2, tzinfo=UTC)
INSTRUMENT = "crypto:benchmark:BTCUSDT"
ORDER_STRIDE = 40


def fp(value: str | int, scale: int = 3) -> FixedPoint:
    return FixedPoint.from_decimal(Decimal(str(value)), scale)


def instrument() -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=INSTRUMENT,
        asset_class=AssetClass.CRYPTO,
        product_type="spot",
        venue="BENCHMARK",
        native_symbol="BTCUSDT",
        base_currency="BTC",
        quote_currency="USDT",
        settlement_currency="USDT",
        price_tick=fp("0.01", 2),
        quantity_step=fp("0.001"),
        contract_multiplier=fp("1", 0),
        calendar_id="24X7",
        effective_from=START - timedelta(days=1),
        available_at=START - timedelta(days=1),
        metadata={
            "min_quantity": "0.001",
            "maker_fee_rate": "0.0002",
            "taker_fee_rate": "0.0005",
        },
    )


def events(count: int) -> tuple[BarEvent, ...]:
    records = []
    for index in range(count):
        at = START + timedelta(milliseconds=index)
        records.append(
            BarEvent(
                event_id=f"event-{index:08d}",
                instrument_id=INSTRUMENT,
                event_time=at,
                received_at=at,
                available_at=at,
                source="benchmark",
                trading_day=date(2026, 1, 2),
                session_id="benchmark-session",
                sequence=index,
                bar_start=at - timedelta(milliseconds=1),
                bar_end=at,
                open_price=fp("100", 2),
                high_price=fp("100", 2),
                low_price=fp("100", 2),
                close_price=fp("100", 2),
                volume=fp("1"),
                is_complete=True,
            )
        )
    return tuple(records)


class NoOrderStrategy:
    def on_event(self, context, event):
        del context, event
        return ()


class PeriodicOrderStrategy:
    def __init__(self, order_stride: int = ORDER_STRIDE) -> None:
        self.order_stride = order_stride

    def on_event(self, context, event):
        index = int(event.event_id.rsplit("-", 1)[1])
        if index % self.order_stride:
            return ()
        return (
            OrderIntent(
                idempotency_key=f"order-{index:08d}",
                account_id=context.account_id,
                strategy_id=context.strategy_id,
                instrument_id=INSTRUMENT,
                side=Side.BUY,
                quantity=fp("0.001"),
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                created_at=event.available_at,
                limit_price=fp("100", 2),
            ),
        )


def engine(strategy) -> DeterministicRunEngine:
    registry = {INSTRUMENT: instrument()}
    ledger = ExactAccountLedger(
        account_id="benchmark-account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp("100000000", 2)},
    )
    return DeterministicRunEngine(
        run_id="m3a-performance",
        account_id="benchmark-account",
        strategy_id="benchmark-strategy",
        strategy=strategy,
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
        matching_model=BarMatchingModel(registry, participation_rate="1"),
        ledger=ledger,
    )


def peak_working_set_bytes() -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_process_memory_info.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        )
        get_process_memory_info.restype = wintypes.BOOL
        if not get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


def measure(name: str, records: tuple[BarEvent, ...], strategy_type, repeat: int) -> dict:
    rates = []
    last_engine = None
    last_result = None
    for _ in range(repeat):
        candidate = engine(strategy_type())
        started = time.perf_counter()
        result = candidate.replay(records, seed=42)
        elapsed = time.perf_counter() - started
        rates.append(len(records) / elapsed)
        last_engine = candidate
        last_result = result
    assert last_engine is not None and last_result is not None
    return {
        "workload": name,
        "events": len(records),
        "orders": last_result.order_count,
        "fills": last_result.fill_count,
        "transactions": len(last_engine.ledger.transactions),
        "events_per_s_runs": [round(rate, 2) for rate in rates],
        "events_per_s_median": round(statistics.median(rates), 2),
        "result_sha256": last_result.result_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-order-events", type=int, default=10_000)
    parser.add_argument("--matching-events", type=int, default=40_000)
    parser.add_argument("--order-stride", type=int, default=ORDER_STRIDE)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--require-rate", type=float, default=50_000)
    args = parser.parse_args()
    if (
        args.no_order_events <= 0
        or args.matching_events <= 0
        or args.repeat <= 0
        or args.order_stride <= 1
    ):
        parser.error("event counts/repeat must be positive and order-stride must exceed one")
    if args.matching_events % args.order_stride:
        parser.error(f"matching-events must be divisible by {args.order_stride}")
    no_order_records = events(args.no_order_events)
    matching_records = events(args.matching_events)
    results = [
        measure("no_orders", no_order_records, NoOrderStrategy, args.repeat),
        measure(
            f"bar_matching_exact_ledger_stride_{args.order_stride}",
            matching_records,
            lambda: PeriodicOrderStrategy(args.order_stride),
            args.repeat,
        ),
    ]
    peak = peak_working_set_bytes()
    for result in results:
        result["peak_working_set_mib"] = round(peak / 1024 / 1024, 2)
        result["rate_gate"] = result["events_per_s_median"] >= args.require_rate
        result["memory_gate"] = peak < 16 * 1024**3
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if all(result["rate_gate"] and result["memory_gate"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
