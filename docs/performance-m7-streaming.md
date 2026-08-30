# M7 bounded replay artifacts and performance gate

## Outcome

The candidate adds an artifact-retention-bounded Arrow path without replacing the frozen
Python reference path. Correctness, compatibility and coverage gates pass. From clean commit
`36c130bef9829fcc6506941478d66f58f2ff73a0`, all three independent10-million-event processes pass
the50,000-events/second and16GiB gates; no calibration result is promoted to release evidence.

## Architecture and compatibility

`DeterministicRunEngine.replay` remains the reference and still materializes `RunArtifacts`.
`replay_to_sink` is an additive migration entry point with these constraints:

- input is a deterministic, already sorted iterator; duplicate IDs, unsorted input and invalid
  event types fail closed;
- `ArrowReplayArtifactSink` writes canonical bytes to typed Arrow IPC batches using one bounded
  producer queue and one writer thread;
- order events, fills and ledger transactions are hashed incrementally in logical sequence order;
- matching batches use sink transactions, so a rejected or failed multi-fill attempt cannot leak
  partial artifacts;
- terminal broker state and replay ledger state are compacted while live orders, balances,
  positions, marks, margin and idempotency semantics remain available;
- `StoredRunArtifacts` exposes lazy byte and JSON iterators; no consumer is forced to reconstruct
  the complete Python object graph;
- incomplete runs retain `FAILED.json`; only a sealed and closed run receives a complete manifest;
- manifest publication is atomic and no-clobber, and strict reload verifies canonical manifest
  bytes, manifest hash, physical file size/hash, Arrow schema, contiguous sequence and logical hash.

The Arrow file stores a monotonic `sequence:int64` and `payload:large_binary`. The payload is the
same canonical JSON byte representation used by the frozen hashes. This is a compatibility format,
not a replacement for `standard/v2`; downstream publication still maps these facts into the shared
v2 schemas and manifest.

## Differential and safety evidence

The differential tests compare the new path with the current Python reference and the frozen
v0.4.1 hashes at byte level for order events, fills and ledger transactions, and at exact fixed-point
level for NAV. The matrix includes:

| Requirement | Evidence |
|---|---|
| A-share/ETF, T+1, price limits, suspension | existing rule/ledger suites plus A-share streamed golden run and suspension path |
| Futures open/close/close-today and daily settlement | existing rule/ledger suites plus futures streamed golden run and explicit settlement artifact |
| Crypto spot/perpetual, funding and margin | crypto streamed golden run, funding/no-position funding and existing margin suites |
| Partial/multiple fills, cancellation, rejection and expiry | broker idempotency, transactional multi-fill commit/rollback, cancel, reject and DAY expiry tests |
| Latency, insufficient liquidity/margin and liquidation boundary | latency, matching, risk and liquidation tests |
| Failure atomicity and deterministic input | writer failure, invalid input, duplicate event, queue saturation and sink transaction tests |

No extra runtime dependency was introduced: `pyarrow` was already a direct, locked dependency and
has Python3.10-3.12 wheels in the existing lock. The unchanged `replay` path is the rollback path.

## Benchmark contract

The certification workload emits one order every20 market events and fills it on the next eligible
event, giving an explicit5% fill density. It exercises strategy dispatch, pre-trade and runtime
risk, matching, fills, fees and exact double-entry ledger posting without pretending that every
market event produces an order. The separate dense stress workload emits one order every two
events (50% fill density) and is not relabelled as the release workload.

Elapsed time includes event materialization, matching, risk, fill, fee, exact fixed-point
double-entry ledger, canonical serialization, Arrow initialization/write/seal, logical hashes,
ledger hash read back and manifest close. Only process startup, strict post-run reload and
construction of one static fixture template are excluded. Strict reload is nevertheless required
to pass and its duration is reported. Every repeat is a fresh process; the rate gate requires every
process, not the median, to reach50,000 events/second. Peak memory is Windows process
`PeakWorkingSetSize` and therefore includes Arrow and retained live replay state.

The official run uses the recorded system temporary directory and writes to a unique directory under
`F:\puresaber-m7-artifacts`. Every Arrow stream and canonical manifest is retained; the benchmark
has no automatic deletion mode. Exact machine, dependency, commit, dirty-state, timing,
output-volume, strict-verification and per-process fields live in the committed JSON evidence.

## Formal result

| Run | Events/s | Peak working set | Strict reload |
|---:|---:|---:|---|
| 1 | 62,920.17 | 2,242.47MiB | PASS |
| 2 | 64,106.74 | 2,233.68MiB | PASS |
| 3 | 61,356.14 | 2,240.03MiB | PASS |

Each run processed10,000,000 events,500,000 fills and1,000,001 exact ledger transactions. All
logical hashes, physical Arrow hashes and the manifest hash were identical across fresh processes.
The three retained artifact directories contain1,501,955,792 bytes each. The machine-readable
evidence is `validation/performance/m7-execution-final-10m.json`; its SHA-256 is
`3bf5f9f18adfcf38489cf0d20d62ee8b561d2f4fc647bbe87bf21f92bab6a90f`.

The Arrow buffers and writer queue are bounded, but replay identity sets and broker order/index
state still scale with event or order count. The claim is therefore controlled memory at the
certified10-million-event envelope (maximum2,242.47MiB), not strict input-independent O(1) memory.

## Dense-stress limitation

The bounded path removes the complete immutable Python artifact graph and materially lowers the
memory slope, but 50%-fill stress remains dominated by per-fill canonical encoding, risk/matching
dispatch and fixed-point ledger posting. Any future native/vectorized hot path must remain a
separately reviewed optimization behind the same public contracts and byte-level Python-oracle
differential suite; M7 does not weaken exact accounting or hide dense-stress results.
