# M3a local performance baseline

Recorded on 2026-08-28 with Python 3.12.5 on an AMD Ryzen 7 7800X3D
(8 cores/16 logical processors) and 31.12 GiB physical memory.

| Workload | Events | Orders/Fills | Throughput | Peak working set | 50k/s gate |
|---|---:|---:|---:|---:|---|
| Deterministic bar-event replay, no orders | 10,000 | 0/0 | 27,502.01 events/s | 136.39 MiB | FAIL |
| Bar matching plus exact ledger | 2,000 | 1,000/1,000 | 6,220.68 events/s | 140.30 MiB | FAIL |

The second workload produced 2,001 balanced ledger transactions. Both benchmarks
exclude fixture construction time and include final artifact hashing. The recorded
result hashes were respectively
`837a621575278f37489a3b8f870801d6e669e9e1b5b2791fded8cc2c90e4658e` and
`981aec1a738d99d07fb4bf9b9768bce002a76d7f5e598d47f39b518abbadf122`.

M3a removed two unbounded history scans by indexing open orders and ledger
idempotency keys. The remaining principal costs are exact snapshot construction,
contract validation, event canonicalization and journal hashing. The 50k-events/s
target remains open performance debt; no threshold, skip or test bypass has been
added. This small local baseline is not a substitute for the planned 10-million-event
L2 certification dataset and must not be presented as that certification.
