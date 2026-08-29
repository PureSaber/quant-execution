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
2,001 balanced transactions. The20,000-event capacity run preserves the same50% density. The
denominator contains only input market events.

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

The same-window candidate is56.78% faster than its immediately preceding v0.4.1 control. The
20,000-event candidate is16.81% faster than the independently reproduced7,895.48 events/s
v0.4.1 capacity baseline. All runs remain far below50,000 events/s, while memory remains far
below16GiB.

## Profile and changes

Replay-only deterministic cProfile runs reduced cumulative profiled time from0.555s to0.505s
for2,000 events and reduced primitive calls from1,273,646 to1,128,655. The retained changes are:

- byte-identical scalar identifier and intent serialization, with Unicode/fallback parity tests;
- direct integer fixed-point balance validation instead of Decimal conversion in transaction
  construction;
- stateless single-order Bar matching and full-quantity reuse without changing fill facts;
- cached immutable asset-rule selection and an exact non-derivative runtime-risk shortcut;
- engine-scoped ledger rollback amortization: every event is still validated and posted, while
  the existing whole-replay checkpoint remains the fail-closed boundary;
- explicit2,000-event v0.4.1 hash assertions and optional JSON output from the benchmark.

The remaining cumulative hotspots are transaction translation/posting and immutable fact
construction, broker lifecycle transitions, repeated open-order risk evaluation, matching/fill
construction, and final canonical ledger serialization. Safely reaching50k/s requires a separately
reviewed compiled or batch accounting kernel with byte-identical fact construction; weakening the
gate is not an acceptable substitute.

## Reproduction and evidence

```bash
python benchmarks/benchmark_replay.py --workload dense --dense-events 2000 \
  --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-optimized-dense-2000.json
python benchmarks/benchmark_replay.py --workload dense --dense-events 20000 \
  --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-optimized-dense-20000.json
```

Both commands intentionally exit nonzero because the rate gate fails. Profile tables and all JSON
runs are committed under`validation/performance/`. This remains a local Bar replay benchmark,
not the planned10-million-event L2 certification.
