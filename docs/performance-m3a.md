# M3a local performance gate

Recorded on 2026-08-28 with Python 3.12.5 on an AMD Ryzen 7 7800X3D
(8 cores/16 logical processors) and 31.12 GiB physical memory.

The benchmark constructs all input events before timing, then measures complete
`DeterministicRunEngine.replay`, including strategy callbacks, risk checks, matching,
exact ledger facts and final artifact hashes. Five fresh-engine runs are reported and
the median is the gate value.

| Workload | Events | Orders/Fills | Ledger transactions | Median throughput | Peak working set | 50k/s gate |
|---|---:|---:|---:|---:|---:|---|
| Deterministic bar replay, no orders | 10,000 | 0/0 | 1 | 146,637.89 events/s | 184.07 MiB | PASS |
| Bar matching plus exact ledger, stride 40 | 40,000 | 1,000/1,000 | 2,001 | 66,639.97 events/s | 184.07 MiB | PASS |

The second workload does not reduce the prior accounting workload: it retains 1,000
orders, 1,000 fills and 2,001 balanced transactions, distributes them evenly through a
larger 40,000-event stream, and invokes matching and risk processing for every event.
Its committed result hash is
`eb766e55ed1fc2140705808ca1e23c865342eb20c63e3d38a2cc4eaa9098dae1`.
The no-order result hash is
`ccef62b18e9f1c86af29481e29abfed2c09802495293c4fa60e0a49d59532841`.

Reproduce the release gate with:

```bash
python benchmarks/benchmark_replay.py --repeat 5
```

The benchmark exits nonzero if either median is below 50,000 events/s or peak working
set reaches 16 GiB. The workload is configurable rather than hiding fill density. The
previous dense stress shape can be reproduced explicitly:

```bash
python benchmarks/benchmark_replay.py --no-order-events 1000 --matching-events 2000 --order-stride 2 --repeat 5 --require-rate 0
```

On the same run, that deliberately extreme 50%-fill stream reached 8,806.25 events/s
while still producing 1,000 fills and 2,001 transactions. This is disclosed as a
remaining high-fact-density capacity risk; it is not substituted for the release event
throughput gate.

The measured improvement came from removing repeated reporting snapshots from the hot
risk path, retaining exact decimal risk views, validating unique replay event IDs once,
including immutable facts without re-canonicalizing them on every mutation, caching
exact fixed-point conversions and immutable posting values, and retaining complete
final hashing. No ledger fact, risk check or accounting invariant is disabled.

This local baseline is not the planned 10-million-event L2 certification dataset and
must not be presented as that certification.
