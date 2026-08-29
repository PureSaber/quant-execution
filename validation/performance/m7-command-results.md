# M7 execution certification handoff

## Scope and acceptance

Scope is limited to `quant-execution`: bounded deterministic replay, immutable Arrow artifacts,
strict artifact verification and the execution performance gate. The path still executes strategy,
pre-trade/runtime risk, matching, fills, fees and exact fixed-point double-entry ledger posting.
It does not add live-order transport or change another repository.

Acceptance requires three independent10-million-event processes from one clean commit; every run
must reach at least50,000 events/second, remain below16GiB peak working set, produce identical
logical and physical hashes, retain its artifacts and pass strict post-run reload.

## Modified files

- Runtime:`src/quant_execution/artifacts.py`, `broker.py`, `engine.py`, `ledger.py`, `__init__.py`.
- Contract/version:`pyproject.toml` (`0.5.0` and replay-artifact manifest schema`1.0.0`).
- Tests/CI:`tests/test_artifacts.py`, `tests/test_benchmark_replay.py`, `.github/workflows/ci.yml`.
- Benchmark/docs:`benchmarks/benchmark_replay.py`, `README.md`,
  `docs/performance-m7-streaming.md`, this handoff and the final JSON report.
- Hygiene:`.gitignore` excludes local virtual environments, coverage JSON and calibration profiles;
  it does not exclude the final certification report.

Rollback is a Git revert of the M7 candidate. The unchanged in-memory `replay` method remains the
runtime compatibility fallback. Historical tags and artifacts are not rewritten.

## Tests and coverage

Locked local environment:

```text
python -m pip check
python -m ruff format --check .
python -m ruff check .
python -m pytest --cov=quant_execution --cov-branch \
  --cov-report=json:coverage.json -q
python -m coverage report --fail-under=80
python tools/check_branch_coverage.py coverage.json --threshold 90 \
  artifacts broker contracts schemas engine matching state_machine ledger rules
```

- `pip check`:PASS.
- Ruff format/check:PASS.
- Python3.12:199 passed; total coverage95.45%.
- Pure branch coverage:artifacts94.74%, broker95.35%, contracts91.88%, schemas94.74%,
  engine91.84%, matching93.25%, state_machine96.67%, ledger91.46%, rules92.38%.
- GitHub Actions run`33250652558`:Python3.10/3.11/3.12 all PASS.
- GitHub Actions run`33250654222`:Python3.10/3.11/3.12 all PASS.

## Formal performance evidence

Source commit:`f41edc86dbd92667312998372c536d4882f8ae8f`.

```text
TEMP=F:\puresaber-m7-temp
TMP=F:\puresaber-m7-temp
python benchmarks/benchmark_replay.py --workload matching \
  --matching-events 10000000 --repeat 3 --require-rate 50000 \
  --memory-limit-gib 16 --artifact-mode arrow \
  --artifact-root F:\puresaber-m7-artifacts\execution-final-10m-f41edc8 \
  --artifact-retention keep --artifact-batch-size 8192 \
  --artifact-queue-batches 2 \
  --output validation\performance\m7-execution-final-10m.json
```

| Run | Events/s | Peak working set | Strict reload | Dirty tree |
|---:|---:|---:|---|---|
| 1 | 61,879.49 | 2,240.21MiB | PASS | false |
| 2 | 63,673.68 | 2,236.92MiB | PASS | false |
| 3 | 63,057.50 | 2,237.29MiB | PASS | false |

- Rate gate:PASS for every run; median63,057.50 events/second.
- Memory gate:PASS; maximum2,240.21MiB.
- Each run:10,000,000 events,500,000 orders/fills,1,000,000 order events and
  1,000,001 balanced ledger transactions; explicit fill density5% (`order_stride=20`).
- Determinism:all logical hashes, every Arrow physical file hash and the manifest hash match across
  all three fresh processes.
- Artifact manifest SHA-256:`c87db59076f852248416df828ff43cbc7dc96cbf196547e97f6e70081c111f27`.
- Final report SHA-256:`089fd422c92dc66222fe41e6594224d8e12c490dfbe2ffe1ef48290302cd0010`.
- Dependencies:Python3.12.5, PyArrow25.0.1, quant-data-kit distribution0.6.1.
- Machine:Windows11,16 logical CPUs; process peak working set includes Arrow and live replay state.
- Retained artifacts:three directories,1,501,955,792 bytes each (about4.20GiB total), under
  `F:\puresaber-m7-artifacts\execution-final-10m-f41edc8`; no file was automatically removed.

The timed interval includes event materialization, matching, risk, fill, fee, exact ledger,
canonical serialization, Arrow initialization/write/seal, logical hashes and manifest close.
Process startup, one static fixture-template construction and strict post-run reload are excluded;
strict reload is independently required and passed in2.98–3.09seconds per run.

## Dense stress and remaining risks

The release workload is explicitly representative rather than adversarial:5% of market events
produce fills. The separate50%-fill dense stress workload remains below50,000 events/second in the
earlier committed evidence and is not relabelled or hidden. It exercises a different capacity
envelope and remains a future optimization target.

Remaining risks:

- formal performance evidence is Windows/Python3.12 host-specific; CI establishes functional and
  coverage compatibility on Python3.10/3.11/3.12 but does not rerun30million events;
- `replay_to_sink` requires deterministically sorted input and stores canonical JSON payloads inside
  Arrow IPC; downstream `standard/v2` publication remains a separate mapping step;
- physical Arrow hashes depend on the locked PyArrow serialization version and must be rebaselined,
  never silently accepted, after a dependency upgrade;
- the dense50%-fill stress gate is still a known capacity limitation;
- this PR must remain unmerged until the independent M7 validator and cross-repository certification
  gate accept its committed evidence.

- PR:[#6](https://github.com/PureSaber/quant-execution/pull/6).
- Final JSON:`validation/performance/m7-execution-final-10m.json`.
