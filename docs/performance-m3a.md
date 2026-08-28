# M3a local performance gate

Recorded on 2026-08-28 with Python 3.12.5 on an AMD Ryzen 7 7800X3D
(8 cores/16 logical processors) and 31.12 GiB physical memory.

The benchmark constructs all input events before timing, then measures complete
`DeterministicRunEngine.replay`, including strategy callbacks, risk checks, matching,
exact ledger facts and final artifact hashes. Every measured run executes in a fresh
child process, so throughput caches and peak working-set values cannot leak between
workloads or repetitions. Three independent runs are reported and the median is the
gate value.

| Workload | Events | Orders/Fills | Ledger transactions | Median throughput | Peak working set | 50k/s gate |
|---|---:|---:|---:|---:|---:|---|
| Deterministic bar replay, no orders | 10,000 | 0/0 | 1 | 163,895.20 events/s | 128.46 MiB | PASS |
| 50%-fill bar matching plus exact ledger | 2,000 | 1,000/1,000 | 2,001 | 10,747.90 events/s | 131.64 MiB | FAIL |

The dense workload has exactly 2,000 market events, 1,000 orders, 1,000 fills, 2,000
order events and 2,001 balanced transactions. It invokes strategy, risk, matching,
ledger mutation and final hashes without padding the denominator with empty events.
Its deterministic result hash is
`e638c2cb3a44b4fe4bb9a234b4451a905a63d8bdf611ef1b6c97042e8dc3efb9`.
The no-order result hash is
`ccef62b18e9f1c86af29481e29abfed2c09802495293c4fa60e0a49d59532841`.

Reproduce the release gate with:

```bash
python benchmarks/benchmark_replay.py --workload all --repeat 3 --require-rate 50000
```

The benchmark exits nonzero if either median is below 50,000 events/s or any independent
worker reaches 16 GiB. A single workload can be reproduced without changing its facts:

```bash
python benchmarks/benchmark_replay.py --workload dense --repeat 3 --require-rate 0
```

The dense median is 39,252.10 events/s below the required threshold, a 78.50% shortfall.
Profiling identifies the cumulative hot path as immutable order/fill/transaction
construction, generic ledger translation/posting, repeated risk and open-order
validation, and final canonical hashing. Low-risk optimizations improved no-order
replay and removed derivative maintenance calculations from cash-only accounts, but
they do not close the dense gap. Closing it now requires a separately reviewed integer
ledger hot path, batch boundary, or compatible native extension; that is a material
architecture expansion rather than a safe M3a defect fix.

No ledger fact, fill, fee, risk check, accounting invariant or final hash is disabled to
improve the reported number. The 50k/s dense gate therefore remains explicitly failed.

This local baseline is not the planned 10-million-event L2 certification dataset and
must not be presented as that certification.
