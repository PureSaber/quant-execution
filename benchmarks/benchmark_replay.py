"""Reproducible M3a replay throughput and independent-process memory gate."""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator
from copy import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from importlib.metadata import version as package_version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pyarrow
from quant_data_kit import AssetClass, BarEvent, FixedPoint, InstrumentSpec

from quant_execution import (
    ArrowReplayArtifactSink,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
    load_stored_artifacts,
)
from quant_execution.broker import DeterministicBroker
from quant_execution.engine import DeterministicRunEngine
from quant_execution.ledger import ExactAccountLedger
from quant_execution.matching import BarMatchingModel
from quant_execution.rules import RuleBookRiskGate

UTC = timezone.utc
START = datetime(2026, 1, 2, tzinfo=UTC)
INSTRUMENT = "crypto:benchmark:BTCUSDT"
CERTIFICATION_ORDER_STRIDE = 20
DENSE_STRESS_ORDER_STRIDE = 2
DENSE_2000_BASELINE = {
    "order_sha256": "b9c9595aab6e650f0e25706ba8813f1e043dfa38a804711981175ed00375feb2",
    "fill_sha256": "692b04a23993a8e2302ae36c262c2115e83c3d26f07fbe8b5b474f43b00705c9",
    "ledger_sha256": "b78ea6ba6de6b2fe9bfc8dee21f7b516149fa8e94732cf93e7e9c5d4610f13b2",
    "result_sha256": "1c43987b6c23db9f77dda68c0d872df51ebfd990eece4eca82ec44fdfccccc9f",
}


def fp(value: str | int, scale: int = 3) -> FixedPoint:
    return FixedPoint.from_decimal(Decimal(str(value)), scale)


DENSE_QUANTITY = fp("0.001")
DENSE_LIMIT_PRICE = fp("100", 2)
BAR_PRICE = fp("100", 2)
BAR_VOLUME = fp("1")
BAR_TEMPLATE = BarEvent(
    event_id="event-template",
    instrument_id=INSTRUMENT,
    event_time=START,
    received_at=START,
    available_at=START,
    source="benchmark",
    trading_day=date(2026, 1, 2),
    session_id="benchmark-session",
    sequence=0,
    bar_start=START - timedelta(milliseconds=1),
    bar_end=START,
    open_price=BAR_PRICE,
    high_price=BAR_PRICE,
    low_price=BAR_PRICE,
    close_price=BAR_PRICE,
    volume=BAR_VOLUME,
    is_complete=True,
)


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


def iter_events(count: int) -> Iterator[BarEvent]:
    for index in range(count):
        at = START + timedelta(milliseconds=index)
        event = copy(BAR_TEMPLATE)
        object.__setattr__(event, "event_id", f"event-{index:08d}")
        object.__setattr__(event, "event_time", at)
        object.__setattr__(event, "received_at", at)
        object.__setattr__(event, "available_at", at)
        object.__setattr__(event, "sequence", index)
        object.__setattr__(event, "bar_start", at - timedelta(milliseconds=1))
        object.__setattr__(event, "bar_end", at)
        yield event


def events(count: int) -> tuple[BarEvent, ...]:
    return tuple(iter_events(count))


class NoOrderStrategy:
    def on_event(self, context, event):
        del context, event
        return ()


class DenseOrderStrategy:
    def __init__(self, order_stride: int) -> None:
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
                quantity=DENSE_QUANTITY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                created_at=event.available_at,
                limit_price=DENSE_LIMIT_PRICE,
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


def worker(
    workload: str,
    event_count: int,
    artifact_mode: str,
    artifact_root: Path | None,
    artifact_retention: str,
    artifact_batch_size: int,
    artifact_queue_batches: int,
    order_stride: int,
) -> int:
    strategy = (
        NoOrderStrategy() if workload == "release_no_orders" else DenseOrderStrategy(order_stride)
    )
    records = events(event_count) if artifact_mode == "memory" else iter_events(event_count)
    candidate = engine(strategy)
    if artifact_mode == "arrow":
        if artifact_root is None:
            raise RuntimeError("Arrow benchmark requires --artifact-root")
        artifact_root.mkdir(parents=True, exist_ok=True)
        run_root = artifact_root.resolve() / f"{workload}-{event_count}-{os.getpid()}"
    else:
        run_root = None
    gc.collect()
    started = time.perf_counter()
    if run_root is None:
        result = candidate.replay(records, seed=42)
    else:
        result = candidate.replay_to_sink(
            records,
            seed=42,
            sink=ArrowReplayArtifactSink(
                run_root,
                batch_size=artifact_batch_size,
                queue_batches=artifact_queue_batches,
            ),
        )
    elapsed = time.perf_counter() - started
    if run_root is None:
        artifacts = candidate.artifacts
        assert artifacts is not None
        order_event_count = len(artifacts.order_events)
        transaction_count = len(artifacts.ledger_transactions)
        artifact_bytes = 0
        artifact_path = None
    else:
        artifacts = candidate.stored_artifacts
        assert artifacts is not None
        order_event_count = artifacts.counts["order_events"]
        transaction_count = artifacts.counts["ledger_transactions"]
        artifact_bytes = sum(
            path.stat().st_size for path in run_root.glob("*.arrow") if path.is_file()
        )
        artifact_path = str(run_root)
        verification_started = time.perf_counter()
        verified = load_stored_artifacts(run_root)
        verification_elapsed = time.perf_counter() - verification_started
        assert verified.counts == artifacts.counts
        assert verified.logical_sha256 == artifacts.logical_sha256
    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    volume_path = run_root if run_root is not None else ROOT
    disk_before_cleanup = shutil.disk_usage(volume_path)
    payload = {
        "pid": os.getpid(),
        "workload": workload,
        "events": event_count,
        "orders": result.order_count,
        "order_events": order_event_count,
        "fills": result.fill_count,
        "transactions": transaction_count,
        "fill_density": result.fill_count / event_count,
        "order_stride": order_stride if workload != "release_no_orders" else None,
        "elapsed_s": elapsed,
        "events_per_s": event_count / elapsed,
        "peak_working_set_mib": peak_working_set_bytes() / 1024 / 1024,
        "order_sha256": result.order_sha256,
        "fill_sha256": result.fill_sha256,
        "ledger_sha256": result.ledger_sha256,
        "result_sha256": result.result_sha256,
        "artifact_mode": artifact_mode,
        "artifact_path": artifact_path,
        "artifact_bytes_before_cleanup": artifact_bytes,
        "artifact_manifest_sha256": (artifacts.manifest_sha256 if run_root is not None else None),
        "artifact_file_sha256": (
            {name: metadata["sha256"] for name, metadata in artifacts.files.items()}
            if run_root is not None
            else {}
        ),
        "artifact_retention": artifact_retention if run_root is not None else "memory",
        "strict_verification_elapsed_s": verification_elapsed if run_root is not None else 0.0,
        "strict_verification_passed": run_root is not None,
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", ""),
            "logical_cpus": os.cpu_count(),
        },
        "python": sys.version,
        "dependencies": {
            "pyarrow": pyarrow.__version__,
            "quant_data_kit": package_version("quant-data-kit"),
        },
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "timing_scope": (
            "includes event materialization, matching, risk, fill, fee, exact ledger, "
            "Arrow sink initialization/write/seal, logical hashes, and manifest close; "
            "excludes process startup and static fixture-template construction"
        ),
        "memory_scope": "process PeakWorkingSetSize including Arrow and retained replay state",
        "temp_directory": os.environ.get("TEMP"),
        "artifact_volume_free_gib_before_cleanup": disk_before_cleanup.free / 1024**3,
    }
    if workload != "release_no_orders":
        expected_orders = event_count // order_stride
        assert payload["orders"] == expected_orders
        assert payload["order_events"] == expected_orders * 2
        assert payload["fills"] == expected_orders
        assert payload["transactions"] == expected_orders * 2 + 1
        assert payload["fill_density"] == 1 / order_stride
        if workload == "dense_matching_exact_ledger" and event_count == 2_000:
            for field, expected in DENSE_2000_BASELINE.items():
                assert payload[field] == expected
    else:
        assert payload["orders"] == payload["order_events"] == payload["fills"] == 0
        assert payload["transactions"] == 1
        assert payload["fill_density"] == 0
    payload["artifact_cleanup"] = "none"
    payload["artifact_files_removed"] = 0
    payload["artifact_volume_free_gib_after_cleanup"] = (
        shutil.disk_usage(volume_path).free / 1024**3
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


def run_once(
    workload: str,
    event_count: int,
    artifact_mode: str,
    artifact_root: Path | None,
    artifact_retention: str,
    artifact_batch_size: int,
    artifact_queue_batches: int,
    order_stride: int,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        workload,
        "--events",
        str(event_count),
        "--artifact-mode",
        artifact_mode,
        "--artifact-retention",
        artifact_retention,
        "--artifact-batch-size",
        str(artifact_batch_size),
        "--artifact-queue-batches",
        str(artifact_queue_batches),
        "--order-stride",
        str(order_stride),
    ]
    if artifact_root is not None:
        command.extend(("--artifact-root", str(artifact_root)))
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
    artifact_mode: str,
    artifact_root: Path | None,
    artifact_retention: str,
    artifact_batch_size: int,
    artifact_queue_batches: int,
    order_stride: int,
) -> dict[str, object]:
    runs = [
        run_once(
            workload,
            event_count,
            artifact_mode,
            artifact_root,
            artifact_retention,
            artifact_batch_size,
            artifact_queue_batches,
            order_stride,
        )
        for _ in range(repeat)
    ]
    hashes = {
        (
            run["order_sha256"],
            run["fill_sha256"],
            run["ledger_sha256"],
            run["result_sha256"],
            run["artifact_manifest_sha256"],
            json.dumps(run["artifact_file_sha256"], sort_keys=True),
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
        "order_stride": representative["order_stride"],
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
        "rate_gate": all(rate >= require_rate for rate in rates),
        "memory_gate": all(peak * 1024**2 < memory_limit_bytes for peak in peaks),
        "artifact_mode": artifact_mode,
        "artifact_paths": [run["artifact_path"] for run in runs],
        "artifact_manifest_sha256": representative["artifact_manifest_sha256"],
        "artifact_file_sha256": representative["artifact_file_sha256"],
        "artifact_bytes_before_cleanup_runs": [
            run["artifact_bytes_before_cleanup"] for run in runs
        ],
        "artifact_retention": [run["artifact_retention"] for run in runs],
        "artifact_cleanup": [run["artifact_cleanup"] for run in runs],
        "artifact_batch_size": artifact_batch_size,
        "artifact_queue_batches": artifact_queue_batches,
        "machine": representative["machine"],
        "python": representative["python"],
        "dependencies": representative["dependencies"],
        "git_commit": representative["git_commit"],
        "git_dirty_runs": [run["git_dirty"] for run in runs],
        "timing_scope": representative["timing_scope"],
        "memory_scope": representative["memory_scope"],
        "temp_directories": [run["temp_directory"] for run in runs],
        "artifact_volume_free_gib_before_cleanup_runs": [
            round(float(run["artifact_volume_free_gib_before_cleanup"]), 2) for run in runs
        ],
        "artifact_volume_free_gib_after_cleanup_runs": [
            round(float(run["artifact_volume_free_gib_after_cleanup"]), 2) for run in runs
        ],
        "artifact_files_removed_runs": [run["artifact_files_removed"] for run in runs],
        "strict_verification_elapsed_s_runs": [
            round(float(run["strict_verification_elapsed_s"]), 6) for run in runs
        ],
        "strict_verification_passed": all(bool(run["strict_verification_passed"]) for run in runs),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workload", choices=("all", "release", "matching", "dense"), default="all"
    )
    parser.add_argument("--release-events", type=int, default=10_000)
    parser.add_argument("--matching-events", type=int, default=10_000_000)
    parser.add_argument("--dense-events", type=int, default=2_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--require-rate", type=float, default=50_000)
    parser.add_argument("--memory-limit-gib", type=float, default=16)
    parser.add_argument(
        "--worker",
        choices=(
            "release_no_orders",
            "matching_exact_ledger",
            "dense_matching_exact_ledger",
        ),
    )
    parser.add_argument("--events", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--artifact-mode", choices=("memory", "arrow"), default="memory")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--artifact-retention", choices=("keep",), default="keep")
    parser.add_argument("--artifact-batch-size", type=int, default=8_192)
    parser.add_argument("--artifact-queue-batches", type=int, default=2)
    parser.add_argument("--order-stride", type=int, default=CERTIFICATION_ORDER_STRIDE)
    args = parser.parse_args()
    if args.worker is not None:
        if args.events is None or args.events <= 0:
            parser.error("worker events must be positive")
        if args.worker != "release_no_orders" and (
            args.order_stride < 2 or args.events % args.order_stride
        ):
            parser.error("matching worker events must be divisible by order stride >= 2")
        return worker(
            args.worker,
            args.events,
            args.artifact_mode,
            args.artifact_root,
            args.artifact_retention,
            args.artifact_batch_size,
            args.artifact_queue_batches,
            args.order_stride,
        )
    if args.repeat < 3:
        parser.error("repeat must be at least three")
    if (
        args.release_events <= 0
        or args.matching_events <= 0
        or args.matching_events % CERTIFICATION_ORDER_STRIDE
        or args.dense_events <= 0
        or args.dense_events % DENSE_STRESS_ORDER_STRIDE
    ):
        parser.error("event counts must be positive and divisible by their fixed order stride")
    if args.artifact_mode == "arrow" and args.artifact_root is None:
        parser.error("--artifact-root is required for --artifact-mode arrow")
    if args.artifact_batch_size <= 0 or args.artifact_queue_batches <= 0:
        parser.error("artifact batch and queue sizes must be positive")
    selected: list[tuple[str, int, int]] = []
    if args.workload in {"all", "release"}:
        selected.append(("release_no_orders", args.release_events, CERTIFICATION_ORDER_STRIDE))
    if args.workload in {"all", "matching"}:
        selected.append(("matching_exact_ledger", args.matching_events, CERTIFICATION_ORDER_STRIDE))
    if args.workload == "dense":
        selected.append(
            ("dense_matching_exact_ledger", args.dense_events, DENSE_STRESS_ORDER_STRIDE)
        )
    memory_limit = int(args.memory_limit_gib * 1024**3)
    results = [
        aggregate(
            name,
            count,
            args.repeat,
            args.require_rate,
            memory_limit,
            args.artifact_mode,
            args.artifact_root,
            args.artifact_retention,
            args.artifact_batch_size,
            args.artifact_queue_batches,
            order_stride,
        )
        for name, count, order_stride in selected
    ]
    encoded = json.dumps(results, indent=2, sort_keys=True)
    print(encoded)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    return 0 if all(item["rate_gate"] and item["memory_gate"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
