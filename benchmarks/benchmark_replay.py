"""Reproducible M3a replay throughput and independent-process memory gate."""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import subprocess
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
DENSE_ORDER_STRIDE = 2


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


class DenseOrderStrategy:
    def on_event(self, context, event):
        index = int(event.event_id.rsplit("-", 1)[1])
        if index % DENSE_ORDER_STRIDE:
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


def worker(workload: str, event_count: int) -> int:
    strategy = NoOrderStrategy() if workload == "release_no_orders" else DenseOrderStrategy()
    records = events(event_count)
    candidate = engine(strategy)
    gc.collect()
    started = time.perf_counter()
    result = candidate.replay(records, seed=42)
    elapsed = time.perf_counter() - started
    artifacts = candidate.artifacts
    assert artifacts is not None
    payload = {
        "pid": os.getpid(),
        "workload": workload,
        "events": event_count,
        "orders": result.order_count,
        "order_events": len(artifacts.order_events),
        "fills": result.fill_count,
        "transactions": len(artifacts.ledger_transactions),
        "fill_density": result.fill_count / event_count,
        "elapsed_s": elapsed,
        "events_per_s": event_count / elapsed,
        "peak_working_set_mib": peak_working_set_bytes() / 1024 / 1024,
        "order_sha256": result.order_sha256,
        "fill_sha256": result.fill_sha256,
        "ledger_sha256": result.ledger_sha256,
        "result_sha256": result.result_sha256,
    }
    if workload == "dense_matching_exact_ledger":
        expected_orders = event_count // DENSE_ORDER_STRIDE
        assert payload["orders"] == expected_orders
        assert payload["order_events"] == expected_orders * 2
        assert payload["fills"] == expected_orders
        assert payload["transactions"] == expected_orders * 2 + 1
        assert payload["fill_density"] == 0.5
    else:
        assert payload["orders"] == payload["order_events"] == payload["fills"] == 0
        assert payload["transactions"] == 1
        assert payload["fill_density"] == 0
    print(json.dumps(payload, sort_keys=True))
    return 0


def run_once(workload: str, event_count: int) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        workload,
        "--events",
        str(event_count),
    ]
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def aggregate(
    workload: str,
    event_count: int,
    repeat: int,
    require_rate: float,
    memory_limit_bytes: int,
) -> dict[str, object]:
    runs = [run_once(workload, event_count) for _ in range(repeat)]
    hashes = {
        (
            run["order_sha256"],
            run["fill_sha256"],
            run["ledger_sha256"],
            run["result_sha256"],
        )
        for run in runs
    }
    if len(hashes) != 1:
        raise RuntimeError(f"{workload} produced non-deterministic hashes")
    rates = [float(run["events_per_s"]) for run in runs]
    peaks = [float(run["peak_working_set_mib"]) for run in runs]
    representative = runs[0]
    median_rate = statistics.median(rates)
    return {
        "workload": workload,
        "events": representative["events"],
        "orders": representative["orders"],
        "order_events": representative["order_events"],
        "fills": representative["fills"],
        "transactions": representative["transactions"],
        "fill_density": representative["fill_density"],
        "independent_processes": repeat,
        "worker_pids": [run["pid"] for run in runs],
        "events_per_s_runs": [round(rate, 2) for rate in rates],
        "events_per_s_median": round(median_rate, 2),
        "peak_working_set_mib_runs": [round(peak, 2) for peak in peaks],
        "peak_working_set_mib": round(max(peaks), 2),
        "order_sha256": representative["order_sha256"],
        "fill_sha256": representative["fill_sha256"],
        "ledger_sha256": representative["ledger_sha256"],
        "result_sha256": representative["result_sha256"],
        "rate_gate": median_rate >= require_rate,
        "memory_gate": max(peaks) * 1024**2 < memory_limit_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", choices=("all", "release", "dense"), default="all")
    parser.add_argument("--release-events", type=int, default=10_000)
    parser.add_argument("--dense-events", type=int, default=2_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--require-rate", type=float, default=50_000)
    parser.add_argument("--memory-limit-gib", type=float, default=16)
    parser.add_argument("--worker", choices=("release_no_orders", "dense_matching_exact_ledger"))
    parser.add_argument("--events", type=int)
    args = parser.parse_args()
    if args.worker is not None:
        if args.events is None or args.events <= 0:
            parser.error("worker events must be positive")
        if args.worker == "dense_matching_exact_ledger" and args.events % 2:
            parser.error("dense worker events must be even")
        return worker(args.worker, args.events)
    if args.repeat < 3:
        parser.error("repeat must be at least three")
    if args.release_events <= 0 or args.dense_events <= 0 or args.dense_events % 2:
        parser.error("event counts must be positive and dense-events must be even")
    selected = []
    if args.workload in {"all", "release"}:
        selected.append(("release_no_orders", args.release_events))
    if args.workload in {"all", "dense"}:
        selected.append(("dense_matching_exact_ledger", args.dense_events))
    memory_limit = int(args.memory_limit_gib * 1024**3)
    results = [
        aggregate(name, count, args.repeat, args.require_rate, memory_limit)
        for name, count in selected
    ]
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0 if all(item["rate_gate"] and item["memory_gate"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
