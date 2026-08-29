# M7 performance command results

Scope:`quant-execution`only. Baseline commit/tag:
`29eccc0e392968b5f7c31976a329605aacce369a`/annotated`v0.4.1`.

## Profile

```text
python -m cProfile ... DeterministicRunEngine.replay(events(2000), seed=42)
```

- v0.4.1 baseline:1,273,646 primitive calls,0.555s cumulative replay time.
- First M7 candidate:1,128,655 primitive calls,0.505s.
- Technical-lead candidate:1,011,749 primitive calls,0.344s.
- Remaining top cumulative paths:`_match_and_commit`0.151s,`_commit_fill`0.112s,
  ledger replay application0.085s, transaction translation0.056s and risk checks0.049s.

## Tests and coverage

```text
python -m ruff check src tests benchmarks tools
python -m ruff format --check src tests benchmarks tools
python -m pytest --cov=quant_execution --cov-branch \
  --cov-report=term-missing --cov-report=json:coverage.json -q
python -m coverage report --fail-under=80
python tools/check_branch_coverage.py coverage.json --threshold 90 \
  broker contracts schemas engine matching state_machine ledger rules
```

- Ruff check/format:PASS.
- Python3.12 pytest:181 passed.
- Total coverage:94.98%.
- Pure branch coverage:broker95.16%,contracts91.88%,schemas92.11%,engine90.15%,
  matching93.25%,state_machine96.67%,ledger90.48%,rules91.43%.
- Local Python3.10/3.11 runtimes were unavailable; PR CI is the required matrix evidence.

## Performance

```text
python benchmarks/benchmark_replay.py --workload all --release-events 100000 \
  --dense-events 2000 --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-techlead-final-all-2000.json
```

- No-order median:240,012.25 events/s;peak:228.40MiB;rate/memory gates:PASS/PASS.
- Dense median:15,912.92 events/s;peak:120.88MiB;rate/memory gates:FAIL/PASS.
- Dense facts:2,000 events,1,000 orders/fills,2,000 order events,2,001 transactions.
- All four dense hashes remain byte-identical to the v0.4.1 golden hashes.

```text
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

| Events | Orders/fills | Order events | Transactions | Median | Peak | Rate gate |
|---:|---:|---:|---:|---:|---:|---|
| 20,000 | 10,000 | 20,000 | 20,001 | 15,869.53/s | 165.54MiB | FAIL |
| 100,000 | 50,000 | 100,000 | 100,001 | 15,536.93/s | 360.31MiB | FAIL |
| 500,000 | 250,000 | 500,000 | 500,001 | 14,638.05/s | 1,319.62MiB | FAIL |

Every row used three fresh processes. Within each row, event, fill, ledger and result hashes were
identical across all three runs. The500,000-event run retained the original50% fill density and
all matching, fee and exact double-entry facts.

The observed100,000-to500,000 incremental working-set slope is2.46KiB/event, implying about
23.5GiB at10million dense events before safety margin. Because throughput was already only29.28%
of target and the measured memory projection exceeded16GiB, a10million run was not started.

## Outcome and next architecture

The candidate is measurably faster and lower-memory than the starting PR candidate, but the M7
release gate remains honestly`FAIL`. The next implementation must introduce a bounded-memory
typed artifact sink and a compiled fixed-point matching/accounting kernel, both guarded by
byte-identical Python-oracle differential tests. Deferring artifact construction outside replay,
dropping transactions or changing event density is prohibited.

- PR:[#6](https://github.com/PureSaber/quant-execution/pull/6).
- Package version remains0.4.1; no merge, tag or release is authorized while the gate fails.
