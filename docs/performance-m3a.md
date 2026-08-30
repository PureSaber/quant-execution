# M7 execution replay performance investigation

Recorded on 2026-08-29 with Python3.12.5 on an AMD Ryzen7 7800X3D
(8 cores/16 logical processors) and31.12GiB physical memory.

## Gate and workload integrity

The benchmark constructs market events before timing and measures the complete
`DeterministicRunEngine.replay` call. The dense workload preserves the public chain:

```text
Strategy.on_event -> RuleBookRiskGate -> BarMatchingModel -> Fill
-> ExactAccountLedger -> canonical order/fill/ledger/result hashes
```

Every measurement runs in a fresh child process. Dense order cadence remains one order for every
two market events: 2,000 events produce exactly1,000 orders, 1,000 fills, 2,000 order events and
2,001 balanced transactions. The20,000, 100,000 and500,000-event capacity runs preserve the same
50% density. The denominator contains only input market events.

The2,000-event benchmark now fails immediately if any v0.4.1 baseline hash changes:

| Fact stream | v0.4.1 SHA-256 |
|---|---|
| Order events | `b9c9595aab6e650f0e25706ba8813f1e043dfa38a804711981175ed00375feb2` |
| Fills | `692b04a23993a8e2302ae36c262c2115e83c3d26f07fbe8b5b474f43b00705c9` |
| Ledger | `b78ea6ba6de6b2fe9bfc8dee21f7b516149fa8e94732cf93e7e9c5d4610f13b2` |
| Result | `1c43987b6c23db9f77dda68c0d872df51ebfd990eece4eca82ec44fdfccccc9f` |

No validation, fee, accounting fact, canonical hash or precision check is disabled. No output is
cached, order density is unchanged, and the event denominator is not padded.

## Reproduced measurements

The former document mixed`10,747.90`and`39,252.10 events/s`; neither is retained as current
evidence. Actual reproductions are below. Short Windows scheduling windows varied materially, so
both the initial runs and the same-window back-to-back control are disclosed.

| Candidate/workload | Events | Median throughput | Peak working set | 50k/s gate |
|---|---:|---:|---:|---|
| v0.4.1 initial baseline | 2,000 | 9,583.10 events/s | 130.23MiB | FAIL |
| v0.4.1 same-window control | 2,000 | 6,807.35 events/s | 129.87MiB | FAIL |
| M7 same-window candidate | 2,000 | 10,672.48 events/s | 129.92MiB | FAIL |
| M7 earlier gate run | 2,000 | 8,341.27 events/s | 130.08MiB | FAIL |
| M7 final all-workload gate | 2,000 | 11,731.93 events/s | 130.24MiB | FAIL |
| M7 capacity run | 20,000 | 9,222.43 events/s | 243.88MiB | FAIL |
| Technical-lead final gate | 2,000 | 15,912.92 events/s | 120.88MiB | FAIL |
| Technical-lead capacity | 20,000 | 15,869.53 events/s | 165.54MiB | FAIL |
| Technical-lead capacity | 100,000 | 15,536.93 events/s | 360.31MiB | FAIL |
| Technical-lead capacity | 500,000 | 14,638.05 events/s | 1,319.62MiB | FAIL |

The technical-lead candidate is35.64% faster than the previous11,731.93 events/s2,000-event
candidate and36.36% faster than the directly reproduced11,637.77 events/s20,000-event starting
point. All retained hashes are unchanged, but all dense runs remain far below50,000 events/s.

The100,000-to500,000 event increment consumes959.31MiB for400,000 more input events, an observed
2.46KiB/event slope. Extrapolating that measured retained-object slope gives approximately23.5GiB
at10million dense events before safety margin. A10million run was therefore not started: it could
not pass the16GiB gate and posed an avoidable host-memory exhaustion risk. This is a measured
capacity FAIL, not a10million certification.

## Profile and changes

Replay-only deterministic cProfile runs reduced cumulative profiled time from0.555s to0.344s
for2,000 events and reduced primitive calls from1,273,646 to1,011,749. The retained changes are:

- byte-identical scalar identifier and intent serialization, with Unicode/fallback parity tests;
- direct integer fixed-point balance validation instead of Decimal conversion in transaction
  construction;
- stateless single-order Bar matching and full-quantity reuse without changing fill facts;
- cached immutable asset-rule selection and an exact non-derivative runtime-risk shortcut;
- engine-scoped ledger rollback amortization: every event is still validated and posted, while
  the existing whole-replay checkpoint remains the fail-closed boundary;
- byte-identical streaming canonical hashes that avoid a duplicate nested dict graph;
- prepared reservation and immutable fee-rate reuse inside the built-in risk gate;
- validated internal immutable order/fill/fee construction without removing lifecycle checks;
- sparse DAY/IOC/FOK indexes, so GTC history does not populate irrelevant expiry state;
- explicit2,000-event v0.4.1 hash assertions and optional JSON output from the benchmark.

The remaining cumulative hotspots are transaction translation/posting, balanced transaction
construction, repeated open-order risk evaluation, matching and retained Python artifact graphs.
Safely reaching both50k/s and10million/<16GiB requires a separately reviewed architecture:

1. a bounded-memory`ReplayArtifactSink`that writes typed Arrow/Parquet record batches while
   preserving order, fill and ledger schema bytes and incremental hashes;
2. a compiled fixed-point matching/accounting kernel with checked integer scales and byte-identical
   transaction IDs, plus Python3.10/3.11/3.12 wheels and a pure-Python equivalence oracle;
3. an explicitly versioned compatibility path for current tuple-based`RunArtifacts`; and
4. golden differential tests over accepted, rejected, partial-fill, fee, funding, settlement and
   rollback paths before the compiled kernel can become the default.

Weakening the gate, dropping facts or moving deferred work outside the timed replay is not an
acceptable substitute.

## Reproduction and evidence

```bash
python benchmarks/benchmark_replay.py --workload dense --dense-events 2000 \
  --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-optimized-dense-2000.json
python benchmarks/benchmark_replay.py --workload dense --dense-events 20000 \
  --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-techlead-final-dense-20000.json
python benchmarks/benchmark_replay.py --workload dense --dense-events 100000 \
  --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-techlead-final-dense-100000.json
python benchmarks/benchmark_replay.py --workload dense --dense-events 500000 \
  --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-techlead-final-dense-500000.json
```

All dense commands intentionally exit nonzero because the rate gate fails. Profile tables and
retained JSON runs are committed under`validation/performance/`. This remains a local Bar replay
benchmark, not the planned10-million-event L2 certification.
