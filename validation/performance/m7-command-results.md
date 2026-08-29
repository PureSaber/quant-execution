# M7 performance command results

Scope:`quant-execution`only. Baseline commit/tag:
`29eccc0e392968b5f7c31976a329605aacce369a`/annotated`v0.4.1`.

## Profile

```text
python -m cProfile ... DeterministicRunEngine.replay(events(2000), seed=42)
```

- Baseline:1,273,646 primitive calls,0.555s cumulative replay time.
- Candidate:1,128,655 primitive calls,0.505s cumulative replay time.
- Full tables:`m7-baseline-profile.txt`,`m7-optimized-profile.txt`.

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
- Pytest:179 passed.
- Total coverage:95.08%.
- Pure branch coverage:broker98.08%,contracts91.77%,schemas92.11%,engine90.32%,
  matching94.31%,state_machine100.00%,ledger90.00%,rules91.26%.

## Performance

```text
python benchmarks/benchmark_replay.py --workload all --release-events 10000 \
  --dense-events 2000 --repeat 3 --require-rate 50000 \
  --output validation/performance/m7-final-all-2000.json
```

- No-order median:202,688.05 events/s;peak:127.15MiB;rate/memory gates:PASS/PASS.
- Dense median:11,731.93 events/s;peak:130.24MiB;rate/memory gates:FAIL/PASS.
- Dense facts:2,000 events,1,000 orders/fills,2,000 order events,2,001 transactions.

```text
python benchmarks/benchmark_replay.py --workload dense --dense-events 2000 \
  --repeat 3 --require-rate 50000 --output validation/performance/m7-optimized-dense-2000.json
```

- Earlier median:8,341.27 events/s;peak:130.08MiB.
- Facts:2,000 events,1,000 orders/fills,2,000 order events,2,001 transactions.
- Memory gate:PASS.Rate gate:FAIL.

```text
python benchmarks/benchmark_replay.py --workload dense --dense-events 20000 \
  --repeat 3 --require-rate 50000 --output validation/performance/m7-optimized-dense-20000.json
```

- Median:9,222.43 events/s;peak:243.88MiB.
- Facts:20,000 events,10,000 orders/fills,20,000 order events,20,001 transactions.
- Memory gate:PASS.Rate gate:FAIL.

The same-window v0.4.1/candidate controls measured6,807.35/10,672.48 events/s. All four
2,000-event hashes are byte-identical. The candidate is therefore semantically equivalent and
measurably faster in the controlled comparison, but it does not satisfy the release rate gate.

Python3.10/3.11/3.12 CI, lock verification, commit, PR and final worktree state are appended after
the remote checks complete.
